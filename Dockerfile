# SAP 智能查询助手 —— 单镜像部署（Day 13-14）
#
# 设计要点：
#   1. 基础镜像用 python:3.12-slim（3.14 太新很多 wheel 没适配；3.12 与本项目依赖
#      langgraph 1.x / sentence-transformers 5.x / chromadb 1.x 全兼容）
#   2. torch 显式安装 CPU 版（PyPI 默认会拉 CUDA 版，~2GB 完全没必要）
#   3. 构建时把 BGE 模型预下载进镜像 + 用 seed_data 生成 SQLite + build_index 建
#      Chroma 索引，运行时即开即用，无首次冷启动延迟、无外网依赖
#   4. .env 不进镜像，运行时通过 --env-file 注入
#   5. 镜像最终 ~1.5GB（torch CPU 占大头）
FROM python:3.12-slim-bookworm

# 系统环境
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    HF_HOME=/app/.cache/huggingface \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# 系统级依赖：编译 chromadb 的 hnswlib 需要 build-essential，装完即删
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# === 第一层：torch CPU 版 ===
# 单独装 + 走 CPU index，避免拉 CUDA 全家桶（默认 PyPI 那个是 GPU 版 ~2GB）
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.4.1

# === 第二层：业务依赖 ===
# requirements.txt 改动时上面的 torch 层仍能复用缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 装完即可卸 build-essential 进一步瘦身（chromadb 已编译好不再需要）
RUN apt-get purge -y build-essential && apt-get autoremove -y

# === 第三层：源代码 ===
COPY src/ ./src/
COPY web/ ./web/
COPY data/__init__.py data/seed_data.py ./data/
COPY scripts/ ./scripts/

# === 第四层：构建期预热 ===
# 1) 预下载 BGE 模型到 HF_HOME，避免首次启动 25s 卡顿 + 部署后无需外网
# 2) 生成 SQLite mock 数据（Faker seed=42，结果可复现）
# 3) 构建 Chroma 向量索引
# 这三步顺序很重要：先下模型，因为 build_index 也需要它
RUN python -c "from sentence_transformers import SentenceTransformer; \
               SentenceTransformer('BAAI/bge-small-zh-v1.5')" \
    && python -m data.seed_data \
    && python -m scripts.build_index

# 创建非 root 用户跑 Streamlit（运行时容器更安全）
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8501

# 健康检查：docker compose 重启策略可用
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "web/app.py"]
