"""数字取证：分区镜像一致切片与污染判定（纯虚构介质建模）。

本包只处理 JSON 描述的虚构区块、事务与采集分片，
不读取任何真实磁盘或个人数据。
"""

__all__ = ["errors", "model", "horn", "engine", "reconstruct", "compare", "suspect", "cli"]
