# OOBNet ONNX model — license and provenance

File: `oobnet.onnx` (67,917,157 bytes)
SHA-256: `18d750a657d498bc15ba10d0458584f90f5ca9ec484a2adb515d64c2e7ea8b58`

## Source

OOBNet (out-of-body detection for endoscopic video, MobileNetV2 + LSTM) by
CAMMA, University of Strasbourg / IHU Strasbourg.

- Project: https://github.com/CAMMA-public/out-of-body-detector
- Original weights: the official TensorFlow `.h5` checkpoint published by the project.

## License

The original weights are licensed under **Creative Commons
Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)** —
**non-commercial research use only**.

This `oobnet.onnx` is a derivative work: the official TensorFlow `.h5` weights
converted to ONNX format, with no retraining or change to the weights. Under the
ShareAlike term it is distributed under the same license, **CC BY-NC-SA 4.0**.

Full license text: https://creativecommons.org/licenses/by-nc-sa/4.0/

Any commercial use requires replacing this model or obtaining permission from the
original authors.

## Conversion

Converted with `pipeline/oob_export_onnx.py` (TensorFlow → ONNX via tf2onnx,
opset 17; input `frames[1,T,64,64,3]`, output `oob_prob[1,T,1]`). Re-running the
script on the same `.h5` reproduces the same SHA-256.

## 中文摘要

本檔為 CAMMA（University of Strasbourg / IHU Strasbourg）OOBNet 官方 TensorFlow
權重轉成 ONNX 的衍生物，權重未重新訓練。原權重授權 CC BY-NC-SA 4.0（僅限非商業
研究），依 ShareAlike 條款本檔沿用同一授權。商業用途須更換模型或取得原作者許可。
使用前須先執行 `surgann fetch-models --only oob` 閱讀並同意授權。
