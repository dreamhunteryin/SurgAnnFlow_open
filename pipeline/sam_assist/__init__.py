"""sam_assist — SAM 輔助標註後端模組（階段一 A1，全後端佈局）。

公開介面（與 annotator server 的接縫契約）：

    from pipeline.sam_assist import get_service, SamNotReady
    svc = get_service()          # lazy singleton；模型缺失/載入失敗 raise SamNotReady
    svc.status()                 # -> {"ready", "model", "device", "detail"}
    svc.predict(video_id, frame, points, labels, box)
                                 # -> {"polygon": [[x,y],...], "score": float}

設計：encoder 每幀跑一次並快取 image embedding，decoder 每次點擊/框選即時推論。
參數集中於 pipeline/configs/sam.yaml。
"""
from .service import SamNotReady, SamService, get_service

__all__ = ["get_service", "SamService", "SamNotReady"]
