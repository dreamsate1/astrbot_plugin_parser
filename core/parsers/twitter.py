import re
from itertools import chain
from typing import Any, ClassVar

from aiohttp import ClientError
from bs4 import BeautifulSoup, Tag

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import ParseResult, Platform, SendGroup
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class TwitterParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="twitter", display_name="推特")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.twitter
        self.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://xdown.app",
                "Referer": "https://xdown.app/",
            }
        )
        self.xdown_url = "https://xdown.app/api/ajaxSearch"
        self.cookiejar = CookieJar(config, self.mycfg, domain="xdown.app")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    async def _req_xdown_api(self, url: str) -> dict[str, Any]:
        async with self.session.post(
            url=self.xdown_url,
            data={"q": url, "lang": "zh-cn"},
            headers=self.headers,
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"xdown API {resp.status} {resp.reason}")
            return await resp.json()

    @handle(
        "twitter.com",
        (
            r"(?<![A-Za-z0-9.-])(?:(?:www|mobile)\.)?twitter\.com/"
            r"(?:[A-Za-z0-9_]+/)*status/\d+"
        ),
    )
    @handle(
        "x.com",
        (
            r"(?<![A-Za-z0-9.-])(?:www\.)?x\.com/"
            r"(?:[A-Za-z0-9_]+/)*status/\d+"
        ),
    )
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        # 从匹配对象中获取原始URL
        url = f"https://{searched.group(0)}"
        try:
            return await self.parse_fxtwitter(url)
        except ParseException:
            # fxtwitter 失败时降级为 xdown（老逻辑，仅图片/视频）
            pass
        resp = await self._req_xdown_api(url)
        if resp.get("status") != "ok":
            raise ParseException("解析失败")

        html_content = resp.get("data")

        if html_content is None:
            raise ParseException("解析失败, 数据为空")

        return self.parse_twitter_html(html_content)

    async def parse_fxtwitter(self, url: str) -> ParseResult:
        """通过 fxtwitter API 解析推文，可渲染带作者/正文/媒体的卡片"""
        import aiohttp

        status_id = ""
        m = re.search(r"(?:status|statuses)/(\d+)", url)
        if m:
            status_id = m.group(1)
        if not status_id:
            raise ParseException("无法提取推文 ID")

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(
                    f"https://api.fxtwitter.com/status/{status_id}"
                ) as resp:
                    if resp.status != 200:
                        raise ParseException(f"fxtwitter API HTTP {resp.status}")
                    data = await resp.json()
        except ParseException:
            raise
        except Exception as e:
            raise ParseException(f"fxtwitter 请求失败: {e}")

        tweet = data.get("tweet")
        if not tweet:
            raise ParseException("fxtwitter 返回数据为空")

        author = tweet.get("author", {})
        text = tweet.get("text", "") or ""
        timestamp = tweet.get("created_timestamp")
        media = tweet.get("media", {})
        photos = media.get("photos", [])
        videos = media.get("videos", [])

        contents: list = []
        for p in photos:
            u = p.get("url", "")
            if u:
                contents.append(self.create_image_contents([u])[0])

        # 视频：取最高码率 variant
        for v in videos:
            variants = v.get("variants", [])
            best = None
            for var in variants:
                if var.get("content_type") == "video/mp4":
                    bitrate = var.get("bitrate") or 0
                    if best is None or bitrate > best[0]:
                        best = (bitrate, var.get("url"))
            if best is None and v.get("url"):
                best = (0, v.get("url"))
            if best:
                contents.append(
                    self.create_video_content(
                        best[1], cover_url=v.get("thumbnail_url") or None
                    )
                )

        author_obj = self.create_author(
            name=author.get("name") or author.get("screen_name") or "推特用户",
            avatar_url=author.get("avatar_url"),
            description=author.get("description"),
        )

        send_groups = [
            SendGroup(contents=[], render_card=True, force_merge=False),
            SendGroup(contents=contents, render_card=False, force_merge=False),
        ]

        return self.result(
            title=None,
            author=author_obj,
            text=text,
            url=url,
            contents=contents,
            timestamp=timestamp,
            send_groups=send_groups,
        )

    def parse_twitter_html(self, html_content: str) -> ParseResult:
        """解析 Twitter HTML 内容

        Args:
            html_content (str): Twitter HTML 内容

        Returns:
            ParseResult: 解析结果
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # 初始化数据
        title = None
        cover_url = None
        video_url = None
        images_urls = []
        dynamic_urls = []

        # 1. 提取缩略图链接
        thumb_tag = soup.find("img")
        if isinstance(thumb_tag, Tag):
            if cover := thumb_tag.get("src"):
                cover_url = str(cover)

        # 2. 提取下载链接
        tw_button_tags = soup.find_all("a", class_="tw-button-dl")
        abutton_tags = soup.find_all("a", class_="abutton")
        for tag in chain(tw_button_tags, abutton_tags):
            if not isinstance(tag, Tag):
                continue
            href = tag.get("href")
            if href is None:
                continue

            href = str(href)
            text = tag.get_text(strip=True)
            if "下载 MP4" in text:
                video_url = href
                break
            elif "下载图片" in text:
                images_urls.append(href)
            elif "下载 gif" in text:
                dynamic_urls.append(href)

        # 3. 提取标题
        title_tag = soup.find("h3")
        if title_tag:
            title = title_tag.get_text(strip=True)

        # 简洁的构建方式
        contents = []

        # 添加视频内容
        if video_url:
            contents.append(self.create_video_content(video_url, cover_url))

        # 添加图片内容
        if images_urls:
            contents.extend(self.create_image_contents(images_urls))

        # 添加动态内容
        if dynamic_urls:
            contents.extend(self.create_dynamic_contents(dynamic_urls))

        return self.result(
            title=title,
            author=self.create_author("无用户名"),
            contents=contents,
        )
        # # 4. 提取Twitter ID
        # twitter_id_input = soup.find("input", {"id": "TwitterId"})
        # if (
        #     twitter_id_input
        #     and isinstance(twitter_id_input, Tag)
        #     and (value := twitter_id_input.get("value"))
        #     and isinstance(value, str)
        # ):
