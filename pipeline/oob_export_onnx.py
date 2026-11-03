#!/usr/bin/env python3
"""OOBNet 一次性轉檔工具：Keras .h5 權重 → ONNX。

一般使用者**不需要**跑這支：ONNX 檔由 `surgann fetch-models --only oob` 下載
（散布＝本 repo GitHub Release 附件；網址與 sha256 記在 pipeline/configs/preprocess.yaml `oob`）。
只有維護者要重新產生 oobnet.onnx 時才用。

★ 只在「含 TensorFlow + tf2onnx 的臨時轉檔 venv」執行（主 venv 不裝 TF）：

    python3 -m venv /tmp/oob_convert_venv
    /tmp/oob_convert_venv/bin/pip install tensorflow-cpu==2.21.0 tf2onnx==1.17.0 onnx onnxruntime
    /tmp/oob_convert_venv/bin/python pipeline/oob_export_onnx.py \\
        --h5 <data 根>/models/oob/oobnet_weights.h5 --out <data 根>/models/oob/oobnet.onnx

本檔刻意**不 import pipeline 套件**（轉檔 venv 沒裝平台依賴），路徑一律由參數給。

轉檔方式：把官方「單幀輸入 + stateful LSTM（跨呼叫保留 h/c）」改寫成等價的非 stateful 序列模型——
  輸入 [1, T, 64, 64, 3] → reshape [T, 64, 64, 3] 餵 MobileNetV2（T 當 batch）→ Flatten → LayerNorm
  → expand_dims(0) → LSTM(640, return_sequences) → LayerNorm → Dense(1, sigmoid) → 輸出 [1, T, 1]。
  權重逐層對應複製（型別與形狀一致才複製）。原模型逐幀 stateful 呼叫 T 次 ≡ 序列模型一次吃 T 幀
  （LSTM 初始狀態皆為 0）。轉出前在 TF 內自檢兩者等價；轉出後以 onnxruntime 對照 TF stateful。
  ★ 圖沒有狀態 I/O：推論端（pipeline/oob_infer.py）必須一次餵整段影片的取樣幀，**禁止分 chunk**。

輸入契約（ONNX）：name=frames shape=[1, T, 64, 64, 3] float32——RGB、bilinear（half-pixel、無 antialias，
  ＝ tf.image.resize 預設）縮到 64×64、`mobilenet_v2.preprocess_input`（x/127.5-1）。
輸出：name=oob_prob shape=[1, T, 1] float32——逐幀體外機率（sigmoid）。
決定論：固定 PYTHONHASHSEED 並正規化節點名稱，同一份 h5 重轉得位元相同的 .onnx（sha256 可重現）。

授權註記：OOBNet 架構與權重 (c) CAMMA, University of Strasbourg / IHU Strasbourg
（https://github.com/CAMMA-public/out-of-body-detector），權重 CC BY-NC-SA 4.0，僅限非商業研究；
轉出的 .onnx 為同一權重的衍生檔，授權相同。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # 轉檔一律 CPU：避免 Keras LSTM 走 cuDNN 核心（無法轉 ONNX）

import numpy as np  # noqa: E402
import tensorflow as tf  # noqa: E402

INPUT_SHAPE = [64, 64]
LSTM_UNITS = 640
DEFAULT_OPSET = 17          # LayerNormalization 自 opset 17 起為原生 op；onnxruntime ≥1.14 支援
INPUT_NAME, OUTPUT_NAME = "frames", "oob_prob"
SELF_CHECK_T = 40           # 自檢序列長度
SELF_CHECK_TOL = 1e-4       # TF stateful vs 序列模型 / vs ONNX 的容許 max|Δ|


def build_stateful_model() -> tf.keras.Model:
    """官方 OOBNet 結構（單幀輸入、stateful LSTM），層序與公開權重一致才能 load_weights。"""
    m = tf.keras.models.Sequential()
    m.add(tf.keras.layers.Input(INPUT_SHAPE + [3], dtype=tf.float32))
    m.add(tf.keras.applications.MobileNetV2(input_shape=INPUT_SHAPE + [3], alpha=1.0,
                                            include_top=False, weights=None))
    m.add(tf.keras.layers.Flatten())
    m.add(tf.keras.layers.LayerNormalization())
    m.add(tf.keras.layers.Dropout(0))
    m.add(tf.keras.layers.Lambda(lambda x: tf.expand_dims(x, 0)))
    m.add(tf.keras.layers.LSTM(units=LSTM_UNITS, return_sequences=True, stateful=True))
    m.add(tf.keras.layers.LayerNormalization())
    m.add(tf.keras.layers.Dense(1, activation="sigmoid"))
    return m


def build_sequence_model() -> tf.keras.Model:
    """等價的非 stateful 序列模型：[1, T, 64, 64, 3] → [1, T, 1]。"""
    inp = tf.keras.layers.Input(shape=(None, *INPUT_SHAPE, 3), batch_size=1, dtype=tf.float32,
                                name=INPUT_NAME)
    x = tf.keras.layers.Lambda(lambda t: tf.reshape(t, [-1, *INPUT_SHAPE, 3]), name="to_frames")(inp)
    x = tf.keras.applications.MobileNetV2(input_shape=INPUT_SHAPE + [3], alpha=1.0,
                                          include_top=False, weights=None)(x)
    x = tf.keras.layers.Flatten()(x)
    x = tf.keras.layers.LayerNormalization()(x)
    x = tf.keras.layers.Lambda(lambda t: tf.expand_dims(t, 0), name="to_sequence")(x)
    x = tf.keras.layers.LSTM(units=LSTM_UNITS, return_sequences=True, stateful=False)(x)
    x = tf.keras.layers.LayerNormalization()(x)
    out = tf.keras.layers.Dense(1, activation="sigmoid", name=OUTPUT_NAME)(x)
    return tf.keras.Model(inp, out, name="oobnet_sequence")


def copy_weights(src: tf.keras.Model, dst: tf.keras.Model) -> int:
    """逐層對應複製（只看有權重的層；型別、權重數與形狀都必須一致）。回傳複製的層數。"""
    a = [l for l in src.layers if l.get_weights()]
    b = [l for l in dst.layers if l.get_weights()]
    if len(a) != len(b):
        raise SystemExit(f"層數不一致：stateful={len(a)} sequence={len(b)}")
    for la, lb in zip(a, b):
        if type(la) is not type(lb):
            raise SystemExit(f"層型別不一致：{type(la).__name__} vs {type(lb).__name__}")
        wa, wb = la.get_weights(), lb.get_weights()
        if [w.shape for w in wa] != [w.shape for w in wb]:
            raise SystemExit(f"{la.name}: 權重形狀不一致 {[w.shape for w in wa]} vs {[w.shape for w in wb]}")
        lb.set_weights(wa)
    return len(a)


def run_stateful(model: tf.keras.Model, x: np.ndarray) -> np.ndarray:
    """官方用法：reset 狀態後逐幀呼叫（batch=1），回傳 [T] 機率。"""
    for layer in model.layers:
        if isinstance(layer, tf.keras.layers.LSTM):
            layer.reset_states()
    return np.asarray([float(model(x[i:i + 1], training=False).numpy()[0, 0, 0])
                       for i in range(x.shape[0])], dtype=np.float32)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonicalize_names(model) -> None:
    """把 tf2onnx 的非決定論命名改成依圖序編號，讓同一 h5 重轉得位元相同的 .onnx。圖 I/O 名稱保留。"""
    counter = {"t": 0, "n": 0}
    keep = {i.name for i in model.graph.input} | {o.name for o in model.graph.output}

    def rename(name: str, mapping: dict) -> str:
        if not name or name in keep:
            return name
        if name not in mapping:
            mapping[name] = f"t{counter['t']}"
            counter["t"] += 1
        return mapping[name]

    def walk(graph, mapping: dict) -> None:
        mapping = dict(mapping)  # 子圖可看見外層名稱；子圖新名稱不外洩
        for vi in list(graph.input) + list(graph.output) + list(graph.value_info):
            vi.name = rename(vi.name, mapping)
        for node in graph.node:  # 名稱依「第一次被引用」的順序編號
            node.name = f"n{counter['n']}_{node.op_type}"
            counter["n"] += 1
            for i, x in enumerate(node.input):
                node.input[i] = rename(x, mapping)
            for i, x in enumerate(node.output):
                node.output[i] = rename(x, mapping)
            for attr in node.attribute:
                if attr.type == attr.GRAPH:
                    walk(attr.g, mapping)
                elif attr.type == attr.GRAPHS:
                    for g in attr.graphs:
                        walk(g, mapping)
        for init in graph.initializer:
            init.name = rename(init.name, mapping)
        inits = sorted(graph.initializer,
                       key=lambda i: int(i.name[1:]) if i.name[1:].isdigit() else -1)
        del graph.initializer[:]
        graph.initializer.extend(inits)

    walk(model.graph, {})


def export(h5: str, out: str, opset: int) -> None:
    import onnx
    import tf2onnx

    h5_sha = sha256_of(h5)
    print(f"tensorflow {tf.__version__}  keras {tf.keras.__version__}  tf2onnx {tf2onnx.__version__}  "
          f"onnx {onnx.__version__}  h5_sha256={h5_sha}")
    stateful = build_stateful_model()
    stateful.load_weights(h5)
    seq = build_sequence_model()
    print(f"權重逐層複製完成：{copy_weights(stateful, seq)} 層")

    # 自檢 1（TF 內）：stateful 逐幀 vs 序列模型一次吃整段
    x = np.random.default_rng(0).standard_normal((SELF_CHECK_T, *INPUT_SHAPE, 3)).astype(np.float32)
    ref = run_stateful(stateful, x)
    got = seq(x[None], training=False).numpy()[0, :, 0]
    diff = float(np.max(np.abs(ref - got)))
    print(f"自檢 1：TF stateful 逐幀 vs 序列模型 max|Δ|={diff:.2e}")
    if diff > SELF_CHECK_TOL:
        raise SystemExit("序列模型與 stateful 原模型不等價，停止匯出")

    spec = (tf.TensorSpec((1, None, *INPUT_SHAPE, 3), tf.float32, name=INPUT_NAME),)
    fn = tf.function(lambda frames: {OUTPUT_NAME: seq(frames, training=False)}, input_signature=spec)
    t0 = time.time()
    model_proto, _ = tf2onnx.convert.from_function(fn, input_signature=spec, opset=opset,
                                                   output_path=None)
    if [o.name for o in model_proto.graph.output] != [OUTPUT_NAME]:
        old = model_proto.graph.output[0].name
        model_proto.graph.output[0].name = OUTPUT_NAME
        for node in model_proto.graph.node:
            for i, o in enumerate(node.output):
                if o == old:
                    node.output[i] = OUTPUT_NAME
    for i in model_proto.graph.input:   # 動態維度統一命名為 T
        i.type.tensor_type.shape.dim[1].dim_param = "T"
    out_dims = model_proto.graph.output[0].type.tensor_type.shape.dim
    out_dims[0].dim_value, out_dims[1].dim_param, out_dims[2].dim_value = 1, "T", 1
    model_proto.doc_string = ("OOBNet (CAMMA, U. Strasbourg; weights CC BY-NC-SA 4.0, non-commercial) "
                              "sequence form; input frames[1,T,64,64,3] float32 preprocessed RGB; "
                              f"output oob_prob[1,T,1]; converted from oobnet_weights.h5 sha256={h5_sha}")
    canonicalize_names(model_proto)
    model_proto.graph.name = "oobnet_sequence"
    onnx.checker.check_model(model_proto)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    onnx.save(model_proto, out)
    print(f"轉檔完成 {time.time() - t0:.1f}s → {out}")
    print(f"  opset={[(i.domain or 'ai.onnx', i.version) for i in model_proto.opset_import]}  "
          f"nodes={len(model_proto.graph.node)}  size={os.path.getsize(out)} bytes")

    # 自檢 2（ONNX）：onnxruntime CPU 對照 TF stateful（同一組輸入）
    try:
        import onnxruntime as ort
    except ImportError:
        print("（未裝 onnxruntime，略過自檢 2）")
    else:
        sess = ort.InferenceSession(out, providers=["CPUExecutionProvider"])
        onx = sess.run([OUTPUT_NAME], {INPUT_NAME: x[None]})[0].reshape(-1)
        d2 = float(np.max(np.abs(ref - onx)))
        print(f"自檢 2：TF stateful vs onnxruntime max|Δ|={d2:.2e}")
        if d2 > SELF_CHECK_TOL:
            raise SystemExit("ONNX 輸出與 TF 原模型不一致")
    print(f"  onnx_sha256={sha256_of(out)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="OOBNet h5 → ONNX 轉檔（只在含 TF 的轉檔 venv 執行）")
    ap.add_argument("--h5", required=True, help="oobnet_weights.h5（CAMMA 官方權重）")
    ap.add_argument("--out", required=True, help="輸出 .onnx 路徑（建議 <data 根>/models/oob/oobnet.onnx）")
    ap.add_argument("--opset", type=int, default=DEFAULT_OPSET)
    args = ap.parse_args()
    if not os.path.isfile(args.h5):
        print(f"error=h5_not_found path={args.h5}", file=sys.stderr)
        return 2
    export(args.h5, args.out, args.opset)
    return 0


if __name__ == "__main__":
    if os.environ.get("PYTHONHASHSEED") != "0":
        # tf2onnx 的 Loop 變數順序受 Python 字串雜湊隨機化影響 → 固定 hash seed 後重啟自己
        os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, "PYTHONHASHSEED": "0"})
    sys.exit(main())
