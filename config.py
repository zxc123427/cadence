"""读 .env 里的配置。

不用装 python-dotenv，十行就够了 —— 设计文档 3.4 的精神：
每加一个依赖都要问问它到底解决了什么问题。
"""

import sys
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"


def _load() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


_env = _load()

BASE_URL = _env.get("CADENCE_BASE_URL", "")
MODEL = _env.get("CADENCE_MODEL", "")
API_KEY = _env.get("CADENCE_API_KEY", "")

# 高德 Web 服务 key。只有查周边地点时才需要，对话本身不需要，
# 所以它不进下面的 check()——缺它不该拦住 chat.py。
AMAP_KEY = _env.get("CADENCE_AMAP_KEY", "")

# 常用坐标。填了之后 nearby.py 就不用每次敲经纬度。
# ⚠️ 高德用 GCJ-02 火星坐标系，跟 GPS 差几百米。
#    坐标直接从高德地图取，别从别的地图抄。
HOME = _env.get("CADENCE_HOME", "")


def check() -> None:
    """配置不全就说清楚缺什么再退出，别让它在调用时报一个看不懂的错。"""
    missing = [n for n, v in
               (("CADENCE_BASE_URL", BASE_URL),
                ("CADENCE_MODEL", MODEL),
                ("CADENCE_API_KEY", API_KEY)) if not v]
    if missing:
        print(f"配置不全，.env 里还缺：{'、'.join(missing)}")
        print(f"文件在：{ENV_PATH}")
        print("打开它，把你那家的三行取消注释并填上 KEY。")
        sys.exit(1)


def check_amap() -> None:
    """单独一个检查：只有用到地点搜索的命令才调它。"""
    if not AMAP_KEY:
        print("没找到 CADENCE_AMAP_KEY。")
        print(f"打开 {ENV_PATH}，加一行：")
        print("    CADENCE_AMAP_KEY=你在 lbs.amap.com 申请的 key")
        print("注意 key 的平台类型要选「Web 服务」，选成 Web端JS 会报错。")
        sys.exit(1)
