# wb2a-panel —— 零依赖，镜像就是一个 Python 基础层 + 三个文件
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="wb2a-panel" \
      org.opencontainers.image.description="workbuddy2api 轻量 Web 管理面板" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY panel.py index.html ./

# 不用 root 跑
RUN adduser -D -u 10001 panel
USER panel

EXPOSE 8321

# 默认连 host.docker.internal 的网关；宿主机把网关端口暴露出来即可。
# auth_dir/bin_dir 只有在把网关目录挂进来时才可用（增强功能）。
ENV WB2A_BASE=http://host.docker.internal:7863 \
    WB2A_PANEL_PORT=8321

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8321/').status==200 else 1)"

CMD ["python3", "panel.py"]
