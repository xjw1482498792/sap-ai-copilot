"""统一读取 .env 配置，全项目通过这个文件拿配置。"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Day 13-14 公网部署用：Web UI 访问密码。
# 留空 = 不启用密码（本地开发 / CLI 评测场景），非空时 Streamlit 前置一个密码门
APP_PASSWORD = os.getenv("APP_PASSWORD")


def require_api_key() -> None:
    """需要调 LLM 的入口（main.py / FastAPI）在执行前调一次，缺 key 立即报错。
    纯数据库脚本（seed_data）不用调，所以 import 不会失败。"""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "缺少 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入你的 key。"
        )

SQLITE_PATH = ROOT / os.getenv("SQLITE_PATH", "data/sap_mock.db").lstrip("./")
