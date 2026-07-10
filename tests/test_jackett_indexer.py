import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from app.helper.sites import SitesHelper
from app.schemas.types import MediaType
from app.helper.jackett import JackettHelper, patch_sites_helper

# Mock 接口数据，用于测试 Jackett 子索引器加载
MOCK_JACKETT_INDEXERS_JSON = [
    {
        "id": "ygo",
        "name": "YGO",
        "type": "public"
    },
    {
        "id": "mteam",
        "name": "M-Team",
        "type": "private"
    }
]

# Mock Torznab XML 响应内容
MOCK_TORZNAB_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Jackett Search Results</title>
    <item>
      <title>Test Movie 2026 1080p WebRip x264</title>
      <description>This is a test movie description</description>
      <guid>https://test.com/details/123</guid>
      <comments>https://test.com/details/123</comments>
      <pubDate>Thu, 09 Jul 2026 12:00:00 +0000</pubDate>
      <size>1073741824</size>
      <enclosure url="https://test.com/download/123.torrent" length="1073741824" type="application/x-bittorrent" />
      <torznab:attr name="seeders" value="15" />
      <torznab:attr name="peers" value="5" />
      <torznab:attr name="downloadvolumefactor" value="0.0" />
      <torznab:attr name="uploadvolumefactor" value="1.0" />
      <torznab:attr name="imdbid" value="1234567" />
    </item>
  </channel>
</rss>
"""


def test_jackett_config_loading():
    """
    测试 Jackett 辅助类的配置获取逻辑
    """
    helper = JackettHelper()
    
    # 用 patch 注入环境变量进行测试
    with patch("app.helper.jackett.settings") as mock_settings:
        mock_settings.JACKETT_HOST = "localhost:9117"
        mock_settings.JACKETT_API_KEY = "mockkey"
        
        host, api_key, password = helper.get_config()
        assert host == "http://localhost:9117/"
        assert api_key == "mockkey"
        assert password is None


def test_jackett_indexers_retrieval(monkeypatch):
    """
    测试从 Jackett 拉取子索引器列表的请求和转换逻辑
    """
    helper = JackettHelper()
    
    # 模拟 get_config 接口
    monkeypatch.setattr(helper, "get_config", lambda: ("http://localhost:9117/", "mockkey", None))
    
    # Mock RequestUtils().get_res 返回的值
    mock_res = MagicMock()
    mock_res.json.return_value = MOCK_JACKETT_INDEXERS_JSON
    
    with patch("app.helper.jackett.RequestUtils") as mock_request_utils:
        mock_request_utils.return_value.get_res.return_value = mock_res
        
        indexers = helper.get_indexers()
        
        assert len(indexers) == 2
        assert indexers[0]["id"] == "jackett_ygo"
        assert indexers[0]["name"] == "Jackett: YGO"
        assert indexers[0]["domain"] == "ygo.jackett.local"
        assert indexers[0]["public"] is True
        assert indexers[0]["parser"] == "Jackett"


def test_torznab_xml_parsing():
    """
    测试 Torznab 协议的 XML 数据流提取和 TorrentInfo 结构转化能力
    """
    helper = JackettHelper()
    results = helper._parse_torznabxml(MOCK_TORZNAB_XML)
    
    assert len(results) == 1
    movie = results[0]
    assert movie["title"] == "Test Movie 2026 1080p WebRip x264"
    assert movie["enclosure"] == "https://test.com/download/123.torrent"
    assert movie["size"] == 1073741824
    assert movie["seeders"] == 15
    assert movie["peers"] == 5
    assert movie["freeleech"] is True
    assert movie["downloadvolumefactor"] == 0.0
    assert movie["uploadvolumefactor"] == 1.0
    assert movie["imdbid"] == "1234567"
    assert movie["page_url"] == "https://test.com/details/123"


def test_sites_helper_monkeypatching(monkeypatch):
    """
    测试挂载 patch_sites_helper 补丁对 SitesHelper 的功能劫持扩展
    """
    # 挂载补丁
    patch_sites_helper()
    
    # Mock Jackett 站点列表
    mock_jackett_indexers = [
        {
            "id": "jackett_mock",
            "name": "Jackett: Mock",
            "domain": "mock.jackett.local",
            "url": "http://mock/torznab/",
            "public": False,
            "parser": "Jackett"
        }
    ]
    
    # 劫持 JackettHelper.get_indexers
    monkeypatch.setattr(JackettHelper, "get_indexers", lambda self: mock_jackett_indexers)
    
    # 模拟公开站点列表包含 1 个站点
    mock_public_indexers = [
        {
            "id": "nyaa",
            "name": "Nyaa",
            "domain": "nyaa.si",
            "public": True
        }
    ]
    monkeypatch.setattr(JackettHelper, "get_public_indexers", lambda self: mock_public_indexers)
    
    # 验证 SitesHelper().get_indexers() 是否成功包含了这批注入站点
    helper = SitesHelper()
    indexers = helper.get_indexers()
    
    # 确保注入成功
    site_ids = [x.get("id") for x in indexers]
    assert "jackett_mock" in site_ids
    assert "nyaa" in site_ids
    
    # 验证 get_indexer 是否能根据虚拟域名匹配到子站点
    indexer = helper.get_indexer("mock.jackett.local")
    assert indexer is not None
    assert indexer["id"] == "jackett_mock"
    assert indexer["parser"] == "Jackett"


def test_moduletest_endpoint_for_jackett(monkeypatch):
    """
    测试系统 API 的 moduletest 端点在对 jackett 进行连通性测试时的分发和校验逻辑
    """
    from app.api.endpoints.system import moduletest
    
    # 1. 模拟连接失败（没有拉取到 indexers）
    monkeypatch.setattr(JackettHelper, "get_config", lambda self: ("http://localhost:9117/", "mockkey", None))
    monkeypatch.setattr(JackettHelper, "get_indexers", lambda self: [])
    
    res = moduletest("jackett")
    assert res.success is False
    assert "连接失败" in res.message
    
    # 2. 模拟连接成功
    mock_indexers = [{"id": "ygo", "name": "YGO"}]
    monkeypatch.setattr(JackettHelper, "get_indexers", lambda self: mock_indexers)
    
    res = moduletest("jackett")
    assert res.success is True
    assert "连接测试成功" in res.message
