import asyncio
import logging
import aiomysql
import re
import json
from datetime import datetime
from typing import ClassVar
import zlib
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Callable
from collections import defaultdict
import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator, ConfigDict, field_validator

from ..config_manager import ConfigManager
from .. import models
from .base import BaseScraper, get_season_from_title

scraper_responses_logger = logging.getLogger("scraper_responses")

# --- Pydantic Models for iQiyi Mobile Search API ---

class IqiyiVideoLibMeta(BaseModel):
    douban_id: Optional[int] = Field(None, alias="douban_id")

class IqiyiSearchVideoInfo(BaseModel):
    item_link: str = Field(alias="itemLink")

class IqiyiSearchAlbumInfo(BaseModel):
    album_id: Optional[int] = Field(None, alias="albumId")
    item_total_number: Optional[int] = Field(None, alias="itemTotalNumber")
    site_id: Optional[str] = Field(None, alias="siteId")
    album_link: Optional[str] = Field(None, alias="albumLink")
    video_doc_type: int = Field(alias="videoDocType")
    album_title: Optional[str] = Field(None, alias="albumTitle")
    channel: Optional[str] = None
    release_date: Optional[str] = Field(None, alias="releaseDate")
    album_img: Optional[str] = Field(None, alias="albumImg")
    video_lib_meta: Optional[IqiyiVideoLibMeta] = Field(None, alias="video_lib_meta")
    videoinfos: Optional[List[IqiyiSearchVideoInfo]] = None

    @property
    def link_id(self) -> Optional[str]:
        link_to_parse = self.album_link
        if self.videoinfos and self.videoinfos[0].item_link and self.album_link:
            link_to_parse = self.videoinfos[0].item_link

        match = re.search(r"v_(\w+?)\.html", link_to_parse)
        return match.group(1).strip() if match else None

    @property
    def year(self) -> Optional[int]:
        if self.release_date and len(self.release_date) >= 4:
            try:
                return int(self.release_date[:4])
            except ValueError:
                return None
        return None

class IqiyiAlbumDoc(BaseModel):
    score: float
    album_doc_info: IqiyiSearchAlbumInfo = Field(alias="albumDocInfo")

class IqiyiSearchDoc(BaseModel):
    docinfos: List[IqiyiAlbumDoc]

class IqiyiSearchResult(BaseModel):
    data: IqiyiSearchDoc

# --- Pydantic Models for iQiyi Desktop Search API (New) ---

class IqiyiDesktopSearchVideo(BaseModel):
    title: str
    pageUrl: Optional[str] = None
    playUrl: Optional[str] = None

class IqiyiDesktopSearchAlbumInfo(BaseModel):
    qipuId: Optional[str] = None
    playQipuId: Optional[str] = None
    title: Optional[str] = None
    channel: Optional[str] = None
    pageUrl: Optional[str] = None
    playUrl: Optional[str] = None
    img: Optional[str] = None
    imgH: Optional[str] = None
    btnText: Optional[str] = None
    videos: Optional[List[IqiyiDesktopSearchVideo]] = None
    year: Optional[Dict[str, Any]] = None
    actors: Optional[Dict[str, Any]] = None
    directors: Optional[Dict[str, Any]] = None

    @field_validator('qipuId', 'playQipuId', mode='before')
    @classmethod
    def coerce_qipu_ids_to_string(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return str(v)

    @property
    def link_id(self) -> Optional[str]:
        url_to_parse = self.pageUrl or self.playUrl
        if not url_to_parse:
            return None
        match = re.search(r"v_(\w+?)\.html", url_to_parse)
        return match.group(1).strip() if match else None

class IqiyiDesktopSearchTemplate(BaseModel):
    template: int
    albumInfo: Optional[IqiyiDesktopSearchAlbumInfo] = None

class IqiyiDesktopSearchData(BaseModel):
    templates: List[IqiyiDesktopSearchTemplate] = []

class IqiyiDesktopSearchResult(BaseModel):
    data: Optional[IqiyiDesktopSearchData] = None

class IqiyiHtmlAlbumInfo(BaseModel):
    video_count: Optional[int] = Field(None, alias="videoCount")

# 修正：此模型现在用于解析新的 baseinfo API 响应
class IqiyiHtmlVideoInfo(BaseModel):
    # 新增：允许模型通过字段名或别名进行填充，以兼容新旧缓存格式
    model_config = ConfigDict(populate_by_name=True)

    album_id: int = Field(alias="albumId")
    tv_id: Optional[int] = Field(None, alias="tvId")
    video_id: Optional[int] = Field(None, alias="videoId")
    # 修正：新API返回的字段名不同
    video_name: str = Field(alias="name")
    video_url: str = Field(alias="playUrl")
    channel_name: Optional[str] = Field(None, alias="channelName")
    duration: int = Field(alias="durationSec")
    # video_count 不再从此模型获取，但保留字段以兼容旧缓存
    video_count: int = 0 

    @model_validator(mode='after')
    def merge_ids(self) -> 'IqiyiHtmlVideoInfo':
        if self.tv_id is None and self.video_id is not None:
            self.tv_id = self.video_id
        return self

class IqiyiEpisodeInfo(BaseModel):
    tv_id: int = Field(alias="tvId")
    name: str
    order: int
    play_url: str = Field(alias="playUrl")

    @property
    def link_id(self) -> Optional[str]:
        match = re.search(r"v_(\w+?)\.html", self.play_url)
        return match.group(1).strip() if match else None

class IqiyiVideoData(BaseModel):
    epsodelist: List[IqiyiEpisodeInfo]

class IqiyiVideoResult(BaseModel):
    data: IqiyiVideoData

class IqiyiUserInfo(BaseModel):
    uid: str

class IqiyiComment(BaseModel):
    content_id: str = Field(alias="contentId")
    content: str
    show_time: int = Field(alias="showTime")
    color: str
    # user_info 字段在XML中可能不存在，设为可选
    user_info: Optional[IqiyiUserInfo] = Field(None, alias="userInfo")

# --- 新增：用于综艺节目分集获取的模型 ---
class IqiyiAlbumVideoInfo(BaseModel):
    publish_time: int = Field(alias="publishTime")

class IqiyiAlbumBaseInfoData(BaseModel):
    first_video: IqiyiAlbumVideoInfo = Field(alias="firstVideo")
    latest_video: IqiyiAlbumVideoInfo = Field(alias="latestVideo")

class IqiyiAlbumBaseInfoResult(BaseModel):
    data: IqiyiAlbumBaseInfoData

class IqiyiMobileVideo(BaseModel):
    id: int
    vid: str
    short_title: str = Field(alias="shortTitle")
    page_url: str = Field(alias="pageUrl")
    publish_time: int = Field(alias="publishTime")
    duration: str

    @property
    def link_id(self) -> Optional[str]:
        match = re.search(r"v_(\w+?)\.html", self.page_url)
        return match.group(1).strip() if match else None

class IqiyiMobileVideoListData(BaseModel):
    videos: List[IqiyiMobileVideo]

class IqiyiMobileVideoListResult(BaseModel):
    data: IqiyiMobileVideoListData

# --- Main Scraper Class ---

class IqiyiScraper(BaseScraper):
    provider_name = "iqiyi"
    _EPISODE_BLACKLIST_PATTERN = re.compile(r"加更|走心|解忧|纯享", re.IGNORECASE)
    # 新增：合并了JS脚本中的过滤关键词，用于过滤搜索结果中的非正片内容
    _SEARCH_JUNK_TITLE_PATTERN = re.compile(
        r'纪录片|预告|花絮|专访|直拍|直播回顾|加更|走心|解忧|纯享|节点|解读|揭秘|赏析|速看|资讯|访谈|番外|短片|'
        r'拍摄花絮|制作花絮|幕后花絮|未播花絮|独家花絮|花絮特辑|'
        r'预告片|先导预告|终极预告|正式预告|官方预告|'
        r'彩蛋片段|删减片段|未播片段|番外彩蛋|'
        r'精彩片段|精彩看点|精彩回顾|精彩集锦|看点解析|看点预告|'
        r'NG镜头|NG花絮|番外篇|番外特辑|'
        r'制作特辑|拍摄特辑|幕后特辑|导演特辑|演员特辑|'
        r'片尾曲|插曲|主题曲|背景音乐|OST|音乐MV|歌曲MV|'
        r'前季回顾|剧情回顾|往期回顾|内容总结|剧情盘点|精选合集|剪辑合集|混剪视频|'
        r'独家专访|演员访谈|导演访谈|主创访谈|媒体采访|发布会采访|'
        r'抢先看|抢先版|试看版|即将上线',
        re.IGNORECASE
    )

    def __init__(self, pool: aiomysql.Pool, config_manager: ConfigManager):
        super().__init__(pool, config_manager)
        self.mobile_user_agent = "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36 Edg/136.0.0.0"
        self.reg_video_info = re.compile(r'"videoInfo":(\{.+?\}),')
        self.cookies = {"pgv_pvid": "40b67e3b06027f3d","video_platform": "2","vversion_name": "8.2.95","video_bucketid": "4","video_omgid": "0a1ff6bc9407c0b1cff86ee5d359614d"}

    async def close(self):
        """关闭HTTP客户端"""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """
        A wrapper for making requests that lazily initializes the client.
        """
        if self.client is None:
            self.client = await self._create_client(
                headers={
                    "User-Agent": self.mobile_user_agent,
                    "Referer": "https://www.iqiyi.com/",
                },
                cookies=self.cookies,
            )
        return await self.client.request(method, url, **kwargs)

    async def _search_desktop_api(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """使用桌面版API进行搜索 (主API)"""
        self.logger.info(f"爱奇艺 (桌面API): 正在搜索 '{keyword}'...")
        url = "https://mesh.if.iqiyi.com/portal/lw/search/homePageV3"
        params = {
            'key': keyword, 'current_page': '1', 'mode': '1', 'source': 'input',
            'suggest': '', 'pcv': '13.074.22699', 'version': '13.074.22699',
            'pageNum': '1', 'pageSize': '25', 'pu': '', 'u': 'f6440fc5d919dca1aea12b6aff56e1c7',
            'scale': '200', 'token': '', 'userVip': '0', 'conduit': '', 'vipType': '-1',
            'os': '', 'osShortName': 'win10', 'dataType': '', 'appMode': '',
            'ad': json.dumps({"lm":3,"azd":1000000000951,"azt":733,"position":"feed"}),
            'adExt': json.dumps({"r":"2.1.5-ares6-pure"})
        }
        headers = {
            'accept': '*/*', 'origin': 'https://www.iqiyi.com', 'referer': 'https://www.iqiyi.com/',
            'user-agent': 'Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36'
        }

        results = []
        try:
            response = await self._request("GET", url, params=params, headers=headers)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi Desktop Search Response (keyword='{keyword}'): {response.text}")
            response.raise_for_status()
            data = IqiyiDesktopSearchResult.model_validate(response.json())

            if not data.data or not data.data.templates:
                return []

            for template in data.data.templates:
                if not template.albumInfo or template.template not in [101, 102, 103]:
                    continue
                
                album = template.albumInfo
                if not album.title or not album.link_id:
                    continue

                if self._SEARCH_JUNK_TITLE_PATTERN.search(album.title):
                    self.logger.debug(f"爱奇艺 (桌面API): 根据标题黑名单过滤掉 '{album.title}'")
                    continue
                
                if album.btnText == '外站付费播放':
                    self.logger.debug(f"爱奇艺 (桌面API): 过滤掉外站付费播放内容 '{album.title}'")
                    continue

                channel = album.channel or ""
                if "电影" in channel: media_type = "movie"
                elif "电视剧" in channel or "动漫" in channel: media_type = "tv_series"
                else: continue # 只保留电影、电视剧、动漫

                year_str = (album.year or {}).get("value") or (album.year or {}).get("name")
                year = int(year_str) if isinstance(year_str, str) and year_str.isdigit() and len(year_str) == 4 else None

                cleaned_title = re.sub(r'<[^>]+>', '', album.title).replace(":", "：")
                
                provider_search_info = models.ProviderSearchInfo(
                    provider=self.provider_name,
                    mediaId=album.link_id,
                    title=cleaned_title,
                    type=media_type,
                    season=get_season_from_title(cleaned_title),
                    year=year,
                    imageUrl=album.img or album.imgH,
                    episodeCount=len(album.videos) if album.videos else None,
                    currentEpisodeIndex=episode_info.get("episode") if episode_info else None,
                )
                results.append(provider_search_info)
        except Exception as e:
            self.logger.error(f"爱奇艺 (桌面API): 搜索 '{keyword}' 失败: {e}", exc_info=True)
        
        return results

    async def search(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        # 修正：缓存键必须包含分集信息，以区分对同一标题的不同分集搜索
        cache_key_suffix = f"_s{episode_info['season']}e{episode_info['episode']}" if episode_info else ""
        cache_key = f"search_{self.provider_name}_{keyword}{cache_key_suffix}"
        cached_results = await self._get_from_cache(cache_key)
        if cached_results is not None:
            self.logger.info(f"爱奇艺 (合并): 从缓存中命中搜索结果 '{keyword}{cache_key_suffix}'")
            return [models.ProviderSearchInfo.model_validate(r) for r in cached_results]

        # 并行执行两个搜索API
        desktop_task = self._search_desktop_api(keyword, episode_info)
        mobile_task = self._search_mobile_api(keyword, episode_info)
        
        results_lists = await asyncio.gather(desktop_task, mobile_task, return_exceptions=True)
        
        all_results = []
        for i, res_list in enumerate(results_lists):
            api_name = "桌面API" if i == 0 else "移动API"
            if isinstance(res_list, list):
                all_results.extend(res_list)
            elif isinstance(res_list, Exception):
                self.logger.error(f"爱奇艺 ({api_name}): 搜索子任务失败: {res_list}", exc_info=True)

        # 基于 mediaId 去重
        unique_results = list({item.mediaId: item for item in all_results}.values())

        self.logger.info(f"爱奇艺 (合并): 搜索 '{keyword}' 完成，找到 {len(unique_results)} 个唯一结果。")
        if unique_results:
            log_results = "\n".join([f"  - {r.title} (ID: {r.mediaId}, 类型: {r.type}, 年份: {r.year or 'N/A'})" for r in unique_results])
            self.logger.info(f"爱奇艺 (合并): 搜索结果列表:\n{log_results}")
        
        results_to_cache = [r.model_dump() for r in unique_results]
        await self._set_to_cache(cache_key, results_to_cache, 'search_ttl_seconds', 300)
        return unique_results

    async def _search_mobile_api(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """使用移动版API进行搜索 (备用API)"""
        self.logger.info(f"爱奇艺 (移动API): 正在搜索 '{keyword}'...")
        url = f"https://search.video.iqiyi.com/o?if=html5&key={keyword}&pageNum=1&pageSize=20"
        results = []
        try:
            response = await self._request("GET", url)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi Search Response (keyword='{keyword}'): {response.text}")
            response.raise_for_status()
            data = IqiyiSearchResult.model_validate(response.json())

            if not data.data or not data.data.docinfos:
                return []

            for doc in data.data.docinfos:
                if doc.score < 0.7: continue
                
                album = doc.album_doc_info
                if not (album.album_link and "iqiyi.com" in album.album_link and album.site_id == "iqiyi" and album.video_doc_type == 1):
                    continue
                # 修正：增加对 album.channel 的非空检查，并添加对“纪录片”频道的过滤
                if album.channel and ("原创" in album.channel or "教育" in album.channel or "纪录片" in album.channel):
                    self.logger.debug(f"爱奇艺: 根据频道 '{album.channel}' 过滤掉 '{album.album_title}'")
                    continue

                # 新增：根据标题过滤掉非正片内容
                if album.album_title and self._SEARCH_JUNK_TITLE_PATTERN.search(album.album_title):
                    self.logger.debug(f"爱奇艺: 根据标题黑名单过滤掉 '{album.album_title}'")
                    continue

                douban_id = None
                if album.video_lib_meta and album.video_lib_meta.douban_id:
                    douban_id = str(album.video_lib_meta.douban_id)

                link_id = album.link_id
                if not link_id:
                    continue

                channel_name = album.channel.split(',')[0] if album.channel else ""
                media_type = "movie" if channel_name == "电影" else "tv_series"

                current_episode = episode_info.get("episode") if episode_info else None
                cleaned_title = re.sub(r'<[^>]+>', '', album.album_title).replace(":", "：") if album.album_title else "未知标题"
                provider_search_info = models.ProviderSearchInfo(
                    provider=self.provider_name,
                    mediaId=link_id,
                    title=cleaned_title,
                    type=media_type,
                    season=get_season_from_title(cleaned_title),
                    year=album.year,
                    imageUrl=album.album_img,
                    douban_id=douban_id,
                    episodeCount=album.item_total_number,
                    currentEpisodeIndex=current_episode,
                )
                self.logger.debug(f"爱奇艺: 创建的 ProviderSearchInfo: {provider_search_info.model_dump_json(indent=2)}")
                results.append(provider_search_info)
        except Exception as e:
            self.logger.error(f"爱奇艺 (移动API): 搜索 '{keyword}' 失败: {e}", exc_info=True)
        return results

    async def _get_tvid_from_link_id(self, link_id: str) -> Optional[str]:
        """
        新增：使用官方API将视频链接ID解码为tvid。
        这比解析HTML更可靠。
        新增：增加国内API端点作为备用，以提高连接成功率。
        新增：增加tvid缓存，减少不必要的API请求。
        """
        cache_key = f"tvid_{link_id}"
        cached_tvid = await self._get_from_cache(cache_key)
        if cached_tvid:
            self.logger.info(f"爱奇艺: 从缓存中命中 tvid (link_id={link_id})")
            return str(cached_tvid)

        endpoints = [
            f"https://pcw-api.iq.com/api/decode/{link_id}?platformId=3&modeCode=intl&langCode=sg",  # International (main)
            f"https://pcw-api.iqiyi.com/api/decode/{link_id}?platformId=3&modeCode=intl&langCode=sg" # Mainland China (fallback)
        ]

        for i, api_url in enumerate(endpoints):
            try:
                self.logger.info(f"爱奇艺: 正在尝试从端点 #{i+1} 解码 tvid (link_id: {link_id})")
                response = await self._request("GET", api_url)
                if await self._should_log_responses():
                    scraper_responses_logger.debug(f"iQiyi Decode API Response (link_id={link_id}, endpoint=#{i+1}): {response.text}")
                response.raise_for_status()
                data = response.json()
                if data.get("code") in ["A00000", "0"] and data.get("data"):
                    tvid = str(data["data"])
                    self.logger.info(f"爱奇艺: 从端点 #{i+1} 成功解码 tvid。")
                    # 缓存结果。tvid 相对稳定，可以使用与基础信息相同的TTL。
                    await self._set_to_cache(cache_key, tvid, 'base_info_ttl_seconds', 1800)
                    return tvid
                else:
                    self.logger.warning(f"爱奇艺: decode API (端点 #{i+1}) 未成功返回 tvid (link_id: {link_id})。响应: {data}")
                    # Don't return here, let it try the next endpoint
            except Exception as e:
                self.logger.warning(f"爱奇艺: 调用 decode API (端点 #{i+1}) 失败: {e}")
                # Don't re-raise, just continue to the next endpoint
        
        # If all endpoints fail
        self.logger.error(f"爱奇艺: 所有 decode API 端点均调用失败 (link_id: {link_id})。")
        return None

    async def _get_video_base_info(self, link_id: str) -> Optional[IqiyiHtmlVideoInfo]:
        # 修正：缓存键必须包含分集信息，以区分对同一标题的不同分集搜索
        cache_key = f"base_info_{link_id}"
        cached_info = await self._get_from_cache(cache_key)
        if cached_info is not None:
            self.logger.info(f"爱奇艺: 从缓存中命中基础信息 (link_id={link_id})")
            try:
                return IqiyiHtmlVideoInfo.model_validate(cached_info)
            except ValidationError as e:
                self.logger.error(f"爱奇艺: 缓存的基础信息 (link_id={link_id}) 验证失败。这可能是一个陈旧或损坏的缓存。")
                self.logger.error(f"导致验证失败的数据: {cached_info}")
                self.logger.error(f"Pydantic 验证错误: {e}")
                return None

        # 主方案：使用API获取信息
        tvid = await self._get_tvid_from_link_id(link_id)
        if not tvid:
            return None

        url = f"https://pcw-api.iqiyi.com/video/video/baseinfo/{tvid}"
        try:
            response = await self._request("GET", url)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi BaseInfo Response (tvid={tvid}): {response.text}")
            response.raise_for_status()
            data = response.json()
            if data.get("code") != "A00000" or not data.get("data"):
                self.logger.warning(f"爱奇艺: baseinfo API 未成功返回数据 (tvid: {tvid})。响应: {data}")
                return None
            
            video_info = IqiyiHtmlVideoInfo.model_validate(data["data"])

            info_to_cache = video_info.model_dump()
            await self._set_to_cache(cache_key, info_to_cache, 'base_info_ttl_seconds', 1800)
            return video_info
        except Exception as e:
            self.logger.error(f"爱奇艺: 获取或解析 baseinfo 失败 (tvid: {tvid}): {e}", exc_info=True)
            
        # 备用方案：如果API失败，则尝试解析HTML页面
        self.logger.warning(f"爱奇艺: API获取基础信息失败，正在尝试备用方案 (解析HTML)...")
        try:
            url = f"https://m.iqiyi.com/v_{link_id}.html"
            response = await self._request("GET", url)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi HTML Fallback Response (link_id={link_id}): {response.text}")
            response.raise_for_status()
            html_content = response.text
            match = self.reg_video_info.search(html_content)
            if match:
                video_json_str = match.group(1)
                video_info = IqiyiHtmlVideoInfo.model_validate(json.loads(video_json_str))
                self.logger.info(f"爱奇艺: 备用方案成功解析到视频信息 (link_id={link_id})")
                info_to_cache = video_info.model_dump()
                await self._set_to_cache(cache_key, info_to_cache, 'base_info_ttl_seconds', 1800)
                return video_info
        except Exception as fallback_e:
            self.logger.error(f"爱奇艺: 备用方案 (解析HTML) 也失败了: {fallback_e}", exc_info=True)
            return None

    async def _get_tv_episodes(self, album_id: int, size: int = 500) -> List[IqiyiEpisodeInfo]:
        """
        获取剧集列表，实现主/备API端点回退机制以提高成功率。
        优先尝试国际版API，失败则回退到国内版API。
        """
        endpoints = [
            f"https://pcw-api.iq.com/api/album/album/avlistinfo?aid={album_id}&page=1&size={size}",  # 国际版 (主)
            f"https://pcw-api.iqiyi.com/albums/album/avlistinfo?aid={album_id}&page=1&size={size}"  # 国内版 (备)
        ]

        for i, url in enumerate(endpoints):
            try:
                self.logger.info(f"爱奇艺: 正在尝试从端点 #{i+1} 获取剧集列表 (album_id: {album_id})")
                response = await self._request("GET", url)
                if await self._should_log_responses():
                    scraper_responses_logger.debug(f"iQiyi Album List Response (album_id={album_id}, endpoint=#{i+1}): {response.text}")
                response.raise_for_status()
                data = IqiyiVideoResult.model_validate(response.json())
                
                if data.data and data.data.epsodelist:
                    self.logger.info(f"爱奇艺: 从端点 #{i+1} 成功获取 {len(data.data.epsodelist)} 个分集。")
                    return data.data.epsodelist
                
                self.logger.warning(f"爱奇艺: 端点 #{i+1} 未返回分集数据。")
            except Exception as e:
                self.logger.error(f"爱奇艺: 尝试端点 #{i+1} 时发生错误 (album_id: {album_id}): {e}")
        
        self.logger.error(f"爱奇艺: 所有端点均未能获取到剧集列表 (album_id: {album_id})。")
        return []

    async def _get_zongyi_episodes(self, album_id: int) -> List[IqiyiEpisodeInfo]:
        """新增：专门为综艺节目获取分集列表。"""
        self.logger.info(f"爱奇艺: 检测到综艺节目 (album_id={album_id})，使用按月获取策略。")
        try:
            # 1. 获取节目的开播和最新日期
            url = f"https://pcw-api.iqiyi.com/album/album/baseinfo/{album_id}"
            response = await self._request("GET", url)
            response.raise_for_status()
            album_base_info = IqiyiAlbumBaseInfoResult.model_validate(response.json()).data
            start_date = datetime.fromtimestamp(album_base_info.first_video.publish_time / 1000)
            end_date = datetime.fromtimestamp(album_base_info.latest_video.publish_time / 1000)

            # 2. 逐月获取分集
            all_videos: List[IqiyiMobileVideo] = []
            # 标准化 current_date 为当月第一天，以进行安全的月份迭代
            current_date = start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            while (current_date.year, current_date.month) <= (end_date.year, end_date.month):
                year = current_date.year
                month = f"{current_date.month:02d}"
                month_url = f"https://pub.m.iqiyi.com/h5/main/videoList/source/month/?sourceId={album_id}&year={year}&month={month}"
                
                self.logger.debug(f"爱奇艺 (综艺): 正在获取 {year}-{month} 的分集...")
                month_response = await self._request("GET", month_url)
                # 如果某个月份没有数据，API可能返回404或空列表，这都是正常情况
                if month_response.status_code == 200:
                    try:
                        # 响应是 JSONP 格式，需要清理
                        jsonp_text = month_response.text
                        json_text = re.sub(r'^[^{]*\(|\)[^}]*$', '', jsonp_text)
                        month_data = IqiyiMobileVideoListResult.model_validate(json.loads(json_text))
                        if month_data.data and month_data.data.videos:
                            all_videos.extend(month_data.data.videos)
                    except (json.JSONDecodeError, ValidationError) as e:
                        self.logger.warning(f"爱奇艺 (综艺): 解析 {year}-{month} 的分集失败: {e}")
                
                # 移至下一个月
                if current_date.month == 12:
                    current_date = current_date.replace(year=current_date.year + 1, month=1)
                else:
                    current_date = current_date.replace(month=current_date.month + 1)
                await asyncio.sleep(0.3) # 礼貌性等待

            # 3. 过滤、排序并转换为标准格式
            filtered_videos = [v for v in all_videos if "精编版" not in v.short_title and "会员版" not in v.short_title]
            filtered_videos.sort(key=lambda v: v.publish_time)

            # 4. 异步获取所有分集的 tvid
            tvid_tasks = [self._get_tvid_from_link_id(v.link_id) for v in filtered_videos if v.link_id]
            tvids = await asyncio.gather(*tvid_tasks)
            
            # 5. 构建最终结果
            final_episodes = []
            for i, video in enumerate(filtered_videos):
                tvid = tvids[i]
                if tvid:
                    final_episodes.append(IqiyiEpisodeInfo(tvId=int(tvid), name=video.short_title, order=i + 1, playUrl=video.page_url))
            
            return final_episodes

        except Exception as e:
            self.logger.error(f"爱奇艺: 获取综艺分集列表失败 (album_id={album_id}): {e}", exc_info=True)
            return []

    async def get_episodes(self, media_id: str, target_episode_index: Optional[int] = None, db_media_type: Optional[str] = None) -> List[models.ProviderEpisodeInfo]:
        cache_key = f"episodes_{media_id}"
        # 仅当不是强制模式（即初次导入）且请求完整列表时才使用缓存
        if target_episode_index is None and db_media_type is None:
            cached_episodes = await self._get_from_cache(cache_key)
            if cached_episodes is not None:
                self.logger.info(f"爱奇艺: 从缓存中命中分集列表 (media_id={media_id})")
                return [models.ProviderEpisodeInfo.model_validate(e) for e in cached_episodes]

        base_info = await self._get_video_base_info(media_id)
        if base_info is None:
            return []

        # 修正：更灵活地决定处理模式
        channel = base_info.channel_name or ""
        is_movie_mode = db_media_type == "movie" or (db_media_type is None and channel == "电影")
        is_zongyi_mode = db_media_type == "tv_series" and channel == "综艺"

        if is_movie_mode:
            # 单集（电影）处理逻辑
            episode_data = {
                "tvId": base_info.tv_id or 0,
                "name": base_info.video_name,
                "order": 1,
                "playUrl": base_info.video_url
            }
            episodes: List[IqiyiEpisodeInfo] = [IqiyiEpisodeInfo.model_validate(episode_data)]
        else:
            # 对于电视剧和综艺节目，优先尝试标准剧集接口
            episodes = await self._get_tv_episodes(base_info.album_id)

            # 如果标准接口未返回任何分集，则尝试使用综艺接口作为备用方案
            if not episodes:
                self.logger.info(f"爱奇艺: 标准剧集接口未返回分集，尝试使用综艺节目接口作为备用方案 (album_id={base_info.album_id})。")
                episodes = await self._get_zongyi_episodes(base_info.album_id)

            if target_episode_index:
                target_episode_from_list = next((ep for ep in episodes if ep.order == target_episode_index), None)
                if target_episode_from_list:
                    episodes = [target_episode_from_list]
                else:
                    self.logger.warning(f"爱奇艺: 目标分集 {target_episode_index} 在获取的列表中未找到 (album_id={base_info.album_id})")
                    return []

            self.logger.debug(f"爱奇艺: 正在为 {len(episodes)} 个分集并发获取真实标题...")
            
            # 修正：将并发请求分批处理，以避免因请求过多而触发API速率限制或导致连接错误。
            # 每次处理5个分集的详情获取，并在批次之间增加1秒的延迟。
            tasks = [self._get_video_base_info(ep.link_id) for ep in episodes if ep.link_id]
            detailed_infos = []
            batch_size = 5
            for i in range(0, len(tasks), batch_size):
                batch = tasks[i:i+batch_size]
                batch_results = await asyncio.gather(*batch, return_exceptions=True)
                detailed_infos.extend(batch_results)
                if i + batch_size < len(tasks):
                    self.logger.debug(f"爱奇艺: 完成一批 ({len(batch)}) 标题获取，等待1秒...")
                    await asyncio.sleep(1)
            
            specific_title_map = {}
            for info in detailed_infos:
                if isinstance(info, IqiyiHtmlVideoInfo) and info.tv_id:
                    specific_title_map[info.tv_id] = info.video_name

            for ep in episodes:
                specific_title = specific_title_map.get(ep.tv_id)
                if specific_title and specific_title != ep.name:
                    self.logger.debug(f"爱奇艺: 标题替换: '{ep.name}' -> '{specific_title}'")
                    ep.name = specific_title

        provider_episodes = [
            models.ProviderEpisodeInfo(
                provider=self.provider_name,
                episodeId=str(ep.tv_id), # Use tv_id for danmaku
                title=ep.name,
                episodeIndex=ep.order,
                url=ep.play_url
            ) for ep in episodes if ep.link_id
        ]

        # 应用自定义黑名单和内置黑名单
        blacklist_pattern = await self.get_episode_blacklist_pattern()
        if blacklist_pattern:
            original_count = len(provider_episodes)
            provider_episodes = [ep for ep in provider_episodes if not blacklist_pattern.search(ep.title)]
            filtered_count = original_count - len(provider_episodes)
            if filtered_count > 0:
                self.logger.info(f"Iqiyi: 根据自定义黑名单规则过滤掉了 {filtered_count} 个分集。")
        
        # 根据黑名单过滤分集
        if self._EPISODE_BLACKLIST_PATTERN:
            original_count = len(provider_episodes)
            provider_episodes = [ep for ep in provider_episodes if not self._EPISODE_BLACKLIST_PATTERN.search(ep.title)]
            filtered_count = original_count - len(provider_episodes)
            if filtered_count > 0:
                self.logger.info(f"Iqiyi: 根据黑名单规则过滤掉了 {filtered_count} 个分集。")

        # 仅当不是强制模式且获取完整列表时才进行缓存
        if target_episode_index is None and db_media_type is None and provider_episodes:
            episodes_to_cache = [e.model_dump() for e in provider_episodes]
            await self._set_to_cache(cache_key, episodes_to_cache, 'episodes_ttl_seconds', 1800)
        return provider_episodes

    async def _get_duration_for_tvid(self, tvid: str) -> Optional[int]:
        """新增：为指定的tvid获取视频时长。"""
        url = f"https://pcw-api.iqiyi.com/video/video/baseinfo/{tvid}"
        try:
            response = await self._request("GET", url)
            response.raise_for_status()
            data = response.json()
            if data.get("code") == "A00000" and data.get("data"):
                return data["data"].get("durationSec")
        except Exception as e:
            self.logger.warning(f"爱奇艺: 获取视频时长失败 (tvid={tvid}): {e}")
        return None

    async def _get_danmu_content_by_mat(self, tv_id: str, mat: int) -> List[IqiyiComment]:
        if len(tv_id) < 4: return []
        
        s1 = tv_id[-4:-2]
        s2 = tv_id[-2:]
        url = f"http://cmts.iqiyi.com/bullet/{s1}/{s2}/{tv_id}_300_{mat}.z"
        
        try:
            response = await self._request("GET", url)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi Danmaku Segment Response (tvId={tv_id}, mat={mat}): status={response.status_code}")
            if response.status_code == 404:
                self.logger.info(f"爱奇艺: 找不到 tvId {tv_id} 的弹幕分段 {mat}，停止获取。")
                return [] # 404 means no more segments
            response.raise_for_status()

            # 根据用户的反馈，恢复为标准的 zlib 解压方式。
            decompressed_data = zlib.decompress(response.content)

            # 增加显式的UTF-8解析器以提高健壮性
            parser = ET.XMLParser(encoding="utf-8")
            root = ET.fromstring(decompressed_data, parser=parser)
            
            comments = []
            # 关键修复：根据日志，弹幕信息在 <bulletInfo> 标签内
            for item in root.findall('.//bulletInfo'):
                content_node = item.find('content')
                show_time_node = item.find('showTime')

                # 核心字段必须存在
                if not (content_node is not None and content_node.text and show_time_node is not None and show_time_node.text):
                    continue
                
                # 安全地获取可选字段
                content_id_node = item.find('contentId')
                color_node = item.find('color')
                user_info_node = item.find('userInfo')
                uid_node = user_info_node.find('uid') if user_info_node is not None else None

                comments.append(IqiyiComment(
                    contentId=content_id_node.text if content_id_node is not None and content_id_node.text else "0",
                    content=content_node.text,
                    showTime=int(show_time_node.text),
                    color=color_node.text if color_node is not None and color_node.text else "ffffff",
                    userInfo=IqiyiUserInfo(uid=uid_node.text) if uid_node is not None and uid_node.text else None
                ))
            return comments
        except zlib.error:
            self.logger.warning(f"爱奇艺: 解压 tvId {tv_id} 的弹幕分段 {mat} 失败，文件可能为空或已损坏。")
        except ET.ParseError:
            self.logger.warning(f"爱奇艺: 解析 tvId {tv_id} 的弹幕分段 {mat} 的XML失败。")
        except Exception as e:
            self.logger.error(f"爱奇艺: 获取 tvId {tv_id} 的弹幕分段 {mat} 时出错: {e}", exc_info=True)
        
        return []

    async def get_comments(self, episode_id: str, progress_callback: Optional[Callable] = None) -> List[dict]:
        tv_id = episode_id # For iqiyi, episodeId is tvId
        all_comments = []
        
        # 优化：先获取视频总时长，以确定需要请求多少个分段
        duration = await self._get_duration_for_tvid(tv_id)
        if duration and duration > 0:
            total_mats = (duration // 300) + 1
            self.logger.info(f"爱奇艺: 视频时长 {duration}s, 预计弹幕分段数: {total_mats}")
        else:
            total_mats = 100 # 如果获取时长失败，回退到旧的固定循环次数
            self.logger.warning(f"爱奇艺: 未能获取视频时长，将使用默认循环次数 ({total_mats})。")

        for mat in range(1, total_mats + 1):
            if progress_callback:
                progress = int((mat / total_mats) * 100) if total_mats > 0 else 100
                await progress_callback(progress, f"正在获取第 {mat}/{total_mats} 分段")

            comments_in_mat = await self._get_danmu_content_by_mat(tv_id, mat)
            if not comments_in_mat:
                break
            all_comments.extend(comments_in_mat)
            await asyncio.sleep(0.2) # Be nice to the server

        if progress_callback:
            await progress_callback(100, "弹幕整合完成")

        return self._format_comments(all_comments)

    def _format_comments(self, comments: List[IqiyiComment]) -> List[dict]:
        if not comments:
            return []

        # 新增：按 content_id 去重
        unique_comments_map: Dict[str, IqiyiComment] = {}
        for c in comments:
            # 保留第一次出现的弹幕
            if c.content_id not in unique_comments_map:
                unique_comments_map[c.content_id] = c
        
        unique_comments = list(unique_comments_map.values())

        # 1. 按内容对弹幕进行分组
        grouped_by_content: Dict[str, List[IqiyiComment]] = defaultdict(list)
        for c in unique_comments: # 使用去重后的列表
            grouped_by_content[c.content].append(c)

        # 2. 处理重复项
        processed_comments: List[IqiyiComment] = []
        for content, group in grouped_by_content.items():
            if len(group) == 1:
                processed_comments.append(group[0])
            else:
                first_comment = min(group, key=lambda x: x.show_time)
                first_comment.content = f"{first_comment.content} X{len(group)}"
                processed_comments.append(first_comment)

        # 3. 格式化处理后的弹幕列表
        formatted = []
        for c in processed_comments:
            mode = 1 # Default scroll
            try:
                color = int(c.color, 16)
            except (ValueError, TypeError):
                color = 16777215 # Default white

            timestamp = float(c.show_time)
            p_string = f"{timestamp:.2f},{mode},{color},[{self.provider_name}]"
            formatted.append({
                "cid": c.content_id,
                "p": p_string,
                "m": c.content,
                "t": timestamp
            })
        return formatted

    async def get_tvid_from_url(self, url: str) -> Optional[str]:
        """
        从爱奇艺视频URL中提取 tvid。
        """
        link_id_match = re.search(r"v_(\w+?)\.html", url)
        if not link_id_match:
            self.logger.warning(f"爱奇艺: 无法从URL中解析出 link_id: {url}")
            return None
        
        link_id = link_id_match.group(1)
        base_info = await self._get_video_base_info(link_id)
        if base_info and base_info.tv_id:
            self.logger.info(f"爱奇艺: 从URL {url} 解析到 tvid: {base_info.tv_id}")
            return str(base_info.tv_id)
        
        self.logger.warning(f"爱奇艺: 未能从 link_id '{link_id}' 获取到 tvid。")
        return None