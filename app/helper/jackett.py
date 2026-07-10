import os
import json
import logging
import requests
import xml.dom.minidom
from pathlib import Path
from typing import List, Tuple, Optional, Any

from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey, MediaType
from app.utils.http import RequestUtils, AsyncRequestUtils
from app.log import logger

# 存储临时加载的 Jackett 子索引器缓存，避免过于频繁发起网络请求
_jackett_indexers_cache: List[dict] = []
_jackett_cache_timestamp: float = 0


class JackettHelper:
    """
    Jackett 索引器和 Torznab 协议辅助类，用于实现多站点检索与解析
    """

    def __init__(self):
        """
        初始化 Jackett 辅助类
        """
        pass

    def get_config(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        获取 Jackett 配置，优先使用系统环境变量，其次使用数据库中的 SystemConfig 设置。
        :return: (host, api_key, password)
        """
        host = settings.JACKETT_HOST
        api_key = settings.JACKETT_API_KEY
        password = None

        if not host or not api_key:
            config = SystemConfigOper().get(SystemConfigKey.Jackett)
            if isinstance(config, dict):
                host = config.get("host") or host
                api_key = config.get("api_key") or api_key
                password = config.get("password")

        if host:
            if not host.startswith("http"):
                host = "http://" + host
            if not host.endswith("/"):
                host = host + "/"

        return host, api_key, password

    def get_indexers(self) -> List[dict]:
        """
        请求 Jackett 服务拉取所有已配置的子索引器，并转换为 MoviePilot 站点字典结构
        :return: 站点字典列表
        """
        global _jackett_indexers_cache, _jackett_cache_timestamp
        import time
        # 缓存 5 分钟，防止重复请求导致延迟
        if _jackett_indexers_cache and (time.time() - _jackett_cache_timestamp < 300):
            return _jackett_indexers_cache

        host, api_key, password = self.get_config()
        if not host or not api_key:
            return []

        cookie = None
        session = requests.session()
        try:
            # 尝试通过登录界面过密码认证获取 cookie
            res = RequestUtils(session=session, timeout=10).post_res(
                url=f"{host}UI/Dashboard", params={"password": password or ""}
            )
            if res and session.cookies:
                cookie = session.cookies.get_dict()
        except Exception as e:
            logger.debug(f"尝试登录 Jackett 失败（如未配置密码可忽略）: {e}")

        indexer_query_url = f"{host}api/v2.0/indexers?configured=true"
        try:
            ret = RequestUtils(cookies=cookie, timeout=15).get_res(indexer_query_url)
            if not ret or not ret.json():
                return []
            
            indexers = []
            for v in ret.json():
                indexer_id = v.get("id")
                indexer_name = v.get("name")
                if not indexer_id or not indexer_name:
                    continue
                
                indexers.append({
                    "id": f"jackett_{indexer_id}",
                    "name": f"Jackett: {indexer_name}",
                    "domain": f"{indexer_id}.jackett.local",
                    "url": f"{host}api/v2.0/indexers/{indexer_id}/results/torznab/",
                    "public": True if v.get("type") == "public" else False,
                    "parser": "Jackett",
                    "cookie": "",
                    "ua": "",
                    "proxy": False,
                    "pri": 100,
                    "downloader": "",
                    "is_active": True
                })
            _jackett_indexers_cache = indexers
            _jackett_cache_timestamp = time.time()
            return indexers
        except Exception as e:
            logger.error(f"从 Jackett 获取站点列表出错: {e}")
            return []

    def get_public_indexers(self) -> List[dict]:
        """
        加载内置 public_sites.json 规则库中的公开 BT 站点，并转换为 MoviePilot 站点字典结构
        :return: 公开站点列表
        """
        json_path = Path(__file__).parent / "public_sites.json"
        if not json_path.exists():
            return []
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                sites = json.load(f)
            
            for site in sites:
                # 补充必要字段，确保在 get_indexers 过滤和激活中正常运作
                site["is_active"] = True
                if "pri" not in site:
                    site["pri"] = 100
            return sites
        except Exception as e:
            logger.error(f"加载内置公开 BT 站点出错: {e}")
            return []

    def search(self, site: dict, keyword: str, mtype: Optional[MediaType] = None, page: int = 0) -> Tuple[bool, List[dict]]:
        """
        同步调用 Torznab XML 接口进行资源检索
        :param site: 站点字典配置
        :param keyword: 检索关键词
        :param mtype: 媒体类型
        :param page: 页码
        :return: (是否发生错误, 结果字典列表)
        """
        host, api_key, _ = self.get_config()
        if not host or not api_key:
            return True, []

        url = site.get("url")
        if not url:
            return True, []

        import urllib.parse
        search_word = urllib.parse.quote(keyword) if keyword else ""
        api_url = f"{url}?apikey={api_key}&t=search&q={search_word}"

        # 添加分类过滤支持
        if mtype == MediaType.MOVIE:
            api_url += "&cat=2000"
        elif mtype == MediaType.TV:
            api_url += "&cat=5000"

        if page > 0:
            api_url += f"&offset={page * 100}&limit=100"

        try:
            logger.info(f"开始请求 Jackett Torznab URL: {api_url}")
            ret = RequestUtils(timeout=20).get_res(api_url)
            if not ret or not ret.text:
                return True, []
            results = self._parse_torznabxml(ret.text)
            return False, results
        except Exception as e:
            logger.error(f"请求 Jackett 站点 {site.get('name')} 出错: {e}")
            return True, []

    async def async_search(self, site: dict, keyword: str, mtype: Optional[MediaType] = None, page: int = 0) -> Tuple[bool, List[dict]]:
        """
        异步调用 Torznab XML 接口进行资源检索
        :param site: 站点字典配置
        :param keyword: 检索关键词
        :param mtype: 媒体类型
        :param page: 页码
        :return: (是否发生错误, 结果字典列表)
        """
        host, api_key, _ = self.get_config()
        if not host or not api_key:
            return True, []

        url = site.get("url")
        if not url:
            return True, []

        import urllib.parse
        search_word = urllib.parse.quote(keyword) if keyword else ""
        api_url = f"{url}?apikey={api_key}&t=search&q={search_word}"

        # 添加分类过滤支持
        if mtype == MediaType.MOVIE:
            api_url += "&cat=2000"
        elif mtype == MediaType.TV:
            api_url += "&cat=5000"

        if page > 0:
            api_url += f"&offset={page * 100}&limit=100"

        try:
            logger.info(f"开始异步请求 Jackett Torznab URL: {api_url}")
            ret = await AsyncRequestUtils(timeout=20).get_res(api_url)
            if not ret or not ret.text:
                return True, []
            results = self._parse_torznabxml(ret.text)
            return False, results
        except Exception as e:
            logger.error(f"异步请求 Jackett 站点 {site.get('name')} 出错: {e}")
            return True, []

    def _parse_torznabxml(self, xml_str: str) -> List[dict]:
        """
        解析 Torznab XML 内容为 MoviePilot 种子字典结构
        :param xml_str: XML 字符串内容
        :return: 种子结果字典列表
        """
        if not xml_str:
            return []

        results = []
        try:
            dom = xml.dom.minidom.parseString(xml_str)
            root = dom.documentElement
            items = root.getElementsByTagName("item")

            def _get_tag_val(tag, name, default=""):
                elems = tag.getElementsByTagName(name)
                if elems and elems[0].firstChild:
                    return elems[0].firstChild.data
                return default

            for item in items:
                title = _get_tag_val(item, "title")
                if not title:
                    continue

                # 种子链接获取
                enclosure_tags = item.getElementsByTagName("enclosure")
                enclosure = ""
                if enclosure_tags:
                    enclosure = enclosure_tags[0].getAttribute("url")
                if not enclosure:
                    continue

                description = _get_tag_val(item, "description")
                size_str = _get_tag_val(item, "size", "0")
                try:
                    size = int(size_str)
                except ValueError:
                    size = 0

                page_url = _get_tag_val(item, "comments")
                seeders = 0
                peers = 0
                freeleech = False
                downloadvolumefactor = 1.0
                uploadvolumefactor = 1.0
                imdbid = ""

                # 解析 torznab 扩展属性
                attrs = item.getElementsByTagName("torznab:attr")
                for attr in attrs:
                    name = attr.getAttribute("name")
                    value = attr.getAttribute("value")
                    if name == "seeders":
                        try:
                            seeders = int(value)
                        except ValueError:
                            pass
                    elif name == "peers":
                        try:
                            peers = int(value)
                        except ValueError:
                            pass
                    elif name == "downloadvolumefactor":
                        try:
                            downloadvolumefactor = float(value)
                            if downloadvolumefactor == 0.0:
                                freeleech = True
                        except ValueError:
                            pass
                    elif name == "uploadvolumefactor":
                        try:
                            uploadvolumefactor = float(value)
                        except ValueError:
                            pass
                    elif name == "imdbid":
                        imdbid = value

                results.append({
                    "title": title,
                    "enclosure": enclosure,
                    "description": description,
                    "size": size,
                    "seeders": seeders,
                    "peers": peers,
                    "freeleech": freeleech,
                    "downloadvolumefactor": downloadvolumefactor,
                    "uploadvolumefactor": uploadvolumefactor,
                    "page_url": page_url,
                    "imdbid": imdbid
                })
        except Exception as e:
            logger.error(f"解析 Torznab XML 出现错误: {e}")

        return results


def patch_sites_helper():
    """
    通过劫持挂载补丁到 SitesHelper 类上，无缝支持 Jackett 索引站点和内置公开 BT 站点
    """
    from app.helper.sites import SitesHelper

    _orig_get_indexers = SitesHelper.get_indexers
    _orig_async_get_indexers = SitesHelper.async_get_indexers
    _orig_get_indexer = SitesHelper.get_indexer
    _orig_async_get_indexer = SitesHelper.async_get_indexer
    _orig_get_indexsites = getattr(SitesHelper, "get_indexsites", None)

    def _get_extra_indexers() -> List[dict]:
        helper = JackettHelper()
        return helper.get_indexers() + helper.get_public_indexers()

    def new_get_indexers(self, *args, **kwargs) -> List[dict]:
        res = _orig_get_indexers(self, *args, **kwargs) or []
        res = list(res)
        extra = _get_extra_indexers()
        return res + extra

    async def new_async_get_indexers(self, *args, **kwargs) -> List[dict]:
        res = await _orig_async_get_indexers(self, *args, **kwargs) or []
        res = list(res)
        extra = _get_extra_indexers()
        return res + extra

    def new_get_indexer(self, domain_or_id: str, *args, **kwargs) -> Optional[dict]:
        if not domain_or_id:
            return None
        extra = _get_extra_indexers()
        for site in extra:
            if site.get("id") == domain_or_id or site.get("domain") == domain_or_id:
                return site
        return _orig_get_indexer(self, domain_or_id, *args, **kwargs)

    async def new_async_get_indexer(self, domain_or_id: str, *args, **kwargs) -> Optional[dict]:
        if not domain_or_id:
            return None
        extra = _get_extra_indexers()
        for site in extra:
            if site.get("id") == domain_or_id or site.get("domain") == domain_or_id:
                return site
        return await _orig_async_get_indexer(self, domain_or_id, *args, **kwargs)

    def new_get_indexsites(self, *args, **kwargs) -> dict:
        res = _orig_get_indexsites(self, *args, **kwargs) or {}
        res = dict(res)
        extra = _get_extra_indexers()
        for s in extra:
            res[s.get("domain")] = {
                "id": s.get("id"),
                "name": s.get("name"),
                "url": s.get("url", ""),
                "public": s.get("public", False)
            }
        return res

    # 实施覆盖方法挂载
    SitesHelper.get_indexers = new_get_indexers
    SitesHelper.async_get_indexers = new_async_get_indexers
    SitesHelper.get_indexer = new_get_indexer
    SitesHelper.async_get_indexer = new_async_get_indexer
    if _orig_get_indexsites:
        SitesHelper.get_indexsites = new_get_indexsites

    logger.info("Jackett & 内置公开 BT 站点补丁已成功挂载至 SitesHelper。")
