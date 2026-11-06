"""HTTP 依赖：从应用状态中取容器，便于测试替换。"""

from fastapi import Request

from workpanda.bootstrap import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container
