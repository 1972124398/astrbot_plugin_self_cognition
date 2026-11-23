"""Verify installation and view discovery through AstrBot in an isolated process."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.core import LogBroker, db_helper
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.dashboard.services.plugin_page_service import PluginPageService


async def verify(source):
    root = Path(os.environ["ASTRBOT_ROOT"])
    sys.path.insert(0, str(root))
    name = "astrbot_plugin_self_cognition"
    origin = "probe:GroupMessage:community"
    scope = "probe-scope"
    core = AstrBotCoreLifecycle(LogBroker(), db_helper)
    with patch("astrbot.core.core_lifecycle.update_llm_metadata", AsyncMock()):
        await core.initialize()
    try:
        assert core.star_context.get_registered_star(name) is None
        manager = core.plugin_manager
        install = (
            manager.install_plugin(source) if source.startswith("https://") else manager.install_plugin_from_file(source)
        )
        await install
        metadata = core.star_context.get_registered_star(name)
        assert metadata and metadata.activated and metadata.star_cls
        assert metadata.config
        for key in ("allowed_origins", "observation_origins", "proactive_origins"):
            assert metadata.config[key] == []
            assert metadata.config.schema[key]["_special"] == "umo"

        page_service = PluginPageService(manager, core_lifecycle=core)
        pages = await page_service.discover_plugin_pages(metadata)
        assert [page.name for page in pages] == ["cognition"]
        assert metadata.i18n["zh-CN"]["views"]["cognition"]["title"] == "认知面板"
        assert (await page_service.resolve_plugin_page_file(metadata, "cognition", "app.js")).is_file()
        assert (await page_service.resolve_plugin_page_file(metadata, "cognition", "style.css")).is_file()

        await db_helper.upsert_umo_alias(origin, "member", "读书会", None)
        handlers = {(path, tuple(methods)): handler for path, handler, methods, _ in core.star_context.registered_web_apis}
        get_sessions = handlers[f"/{name}/sessions", ("GET",)]
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
        with bind_request_context(PluginRequest(request, username="admin")):
            response = await get_sessions()
        assert response.status_code == 200
        assert [(s["umo"], s["display_name"]) for s in json.loads(response.body)["sessions"]] == [(origin, "读书会")]

        instance = metadata.star_cls
        metadata.config.save_config({"allowed_origins": [origin]})
        await instance.memory.call(instance.store.ensure_scope, scope, origin, "default")
        success, error = await manager.reload(name)
        assert success, error
        current = core.star_context.get_registered_star(name)
        assert current and current.star_cls is not instance
        assert current.config["allowed_origins"] == [origin]
        assert await current.star_cls.memory.call(current.star_cls.store.scope, scope)
        assert [page.name for page in await page_service.discover_plugin_pages(current)] == ["cognition"]
    finally:
        await core.stop()


if __name__ == "__main__":
    asyncio.run(verify(sys.argv[1]))
