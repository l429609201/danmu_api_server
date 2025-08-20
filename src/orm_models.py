from __future__ import annotations
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer,
    String, TEXT, TIMESTAMP, UniqueConstraint, DECIMAL, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class Anime(Base):
    __tablename__ = "anime"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[str] = mapped_column(Enum('tv_series', 'movie', 'ova', 'other', name="anime_type"), default='tv_series')
    image_url: Mapped[Optional[str]] = mapped_column(String(512))
    local_image_path: Mapped[Optional[str]] = mapped_column(String(512))
    season: Mapped[int] = mapped_column(Integer, default=1)
    episode_count: Mapped[Optional[int]] = mapped_column(Integer)
    source_url: Mapped[Optional[str]] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), default=datetime.now)

    sources: Mapped[List["AnimeSource"]] = relationship(back_populates="anime", cascade="all, delete-orphan")
    metadata_record: Mapped["AnimeMetadata"] = relationship(back_populates="anime", cascade="all, delete-orphan", uselist=False)
    aliases: Mapped["AnimeAlias"] = relationship(back_populates="anime", cascade="all, delete-orphan", uselist=False)

    __table_args__ = (
        Index('idx_title_fulltext', 'title'),
    )

class AnimeSource(Base):
    __tablename__ = "anime_sources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    anime_id: Mapped[int] = mapped_column(ForeignKey("anime.id", ondelete="CASCADE"))
    provider_name: Mapped[str] = mapped_column(String(50))
    media_id: Mapped[str] = mapped_column(String(255))
    is_favorited: Mapped[bool] = mapped_column(Boolean, default=False)
    incremental_refresh_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    incremental_refresh_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), default=datetime.now)

    anime: Mapped["Anime"] = relationship(back_populates="sources")
    episodes: Mapped[List["Episode"]] = relationship(back_populates="source", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint('anime_id', 'provider_name', 'media_id', name='idx_anime_provider_media_unique'),)

class Episode(Base):
    __tablename__ = "episode"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("anime_sources.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    episode_index: Mapped[int] = mapped_column(Integer)
    provider_episode_id: Mapped[Optional[str]] = mapped_column(String(255))
    source_url: Mapped[Optional[str]] = mapped_column(String(512))
    fetched_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)

    source: Mapped["AnimeSource"] = relationship(back_populates="episodes")
    comments: Mapped[List["Comment"]] = relationship(back_populates="episode", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint('source_id', 'episode_index', name='idx_source_episode_unique'),)

class Comment(Base):
    __tablename__ = "comment"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cid: Mapped[str] = mapped_column(String(255))
    episode_id: Mapped[int] = mapped_column(ForeignKey("episode.id", ondelete="CASCADE"))
    p: Mapped[str] = mapped_column(String(255))
    m: Mapped[str] = mapped_column(TEXT)
    t: Mapped[float] = mapped_column(DECIMAL(10, 2))

    episode: Mapped["Episode"] = relationship(back_populates="comments")

    __table_args__ = (
        UniqueConstraint('episode_id', 'cid', name='idx_episode_cid_unique'),
        Index('idx_episode_time', 'episode_id', 't'),
    )

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    token: Mapped[Optional[str]] = mapped_column(TEXT)
    token_update: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), default=datetime.now)

class Scraper(Base):
    __tablename__ = "scrapers"
    provider_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    use_proxy: Mapped[bool] = mapped_column(Boolean, default=False)

class MetadataSource(Base):
    __tablename__ = "metadata_sources"
    provider_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_aux_search_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    use_proxy: Mapped[bool] = mapped_column(Boolean, default=False)

class AnimeMetadata(Base):
    __tablename__ = "anime_metadata"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    anime_id: Mapped[int] = mapped_column(ForeignKey("anime.id", ondelete="CASCADE"), unique=True)
    tmdb_id: Mapped[Optional[str]] = mapped_column(String(50))
    tmdb_episode_group_id: Mapped[Optional[str]] = mapped_column(String(50))
    imdb_id: Mapped[Optional[str]] = mapped_column(String(50))
    tvdb_id: Mapped[Optional[str]] = mapped_column(String(50))
    douban_id: Mapped[Optional[str]] = mapped_column(String(50))
    bangumi_id: Mapped[Optional[str]] = mapped_column(String(50))

    anime: Mapped["Anime"] = relationship(back_populates="metadata_record")

class Config(Base):
    __tablename__ = "config"
    config_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    config_value: Mapped[str] = mapped_column(TEXT)
    description: Mapped[Optional[str]] = mapped_column(TEXT)

class CacheData(Base):
    __tablename__ = "cache_data"
    cache_provider: Mapped[Optional[str]] = mapped_column(String(50))
    cache_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    cache_value: Mapped[str] = mapped_column(TEXT)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, index=True)

class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    token: Mapped[str] = mapped_column(String(50), unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), default=datetime.now)
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

class TokenAccessLog(Base):
    __tablename__ = "token_access_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(Integer)
    ip_address: Mapped[str] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(TEXT)
    access_time: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    status: Mapped[str] = mapped_column(String(50))
    path: Mapped[Optional[str]] = mapped_column(String(512))

    __table_args__ = (Index('idx_token_id_time', 'token_id', 'access_time', mysql_length={'access_time': None}),)

class UaRule(Base):
    __tablename__ = "ua_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ua_string: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), default=datetime.now)

class BangumiAuth(Base):
    __tablename__ = "bangumi_auth"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bangumi_user_id: Mapped[Optional[int]] = mapped_column(Integer)
    nickname: Mapped[Optional[str]] = mapped_column(String(255))
    avatar_url: Mapped[Optional[str]] = mapped_column(String(512))
    access_token: Mapped[str] = mapped_column(TEXT)
    refresh_token: Mapped[Optional[str]] = mapped_column(TEXT)
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    authorized_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

class OauthState(Base):
    __tablename__ = "oauth_states"
    state_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, index=True)

class AnimeAlias(Base):
    __tablename__ = "anime_aliases"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    anime_id: Mapped[int] = mapped_column(ForeignKey("anime.id", ondelete="CASCADE"), unique=True)
    name_en: Mapped[Optional[str]] = mapped_column(String(255))
    name_jp: Mapped[Optional[str]] = mapped_column(String(255))
    name_romaji: Mapped[Optional[str]] = mapped_column(String(255))
    alias_cn_1: Mapped[Optional[str]] = mapped_column(String(255))
    alias_cn_2: Mapped[Optional[str]] = mapped_column(String(255))
    alias_cn_3: Mapped[Optional[str]] = mapped_column(String(255))

    anime: Mapped["Anime"] = relationship(back_populates="aliases")

class TmdbEpisodeMapping(Base):
    __tablename__ = "tmdb_episode_mapping"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tmdb_tv_id: Mapped[int] = mapped_column(Integer)
    tmdb_episode_group_id: Mapped[str] = mapped_column(String(50))
    tmdb_episode_id: Mapped[int] = mapped_column(Integer)
    tmdb_season_number: Mapped[int] = mapped_column(Integer)
    tmdb_episode_number: Mapped[int] = mapped_column(Integer)
    custom_season_number: Mapped[int] = mapped_column(Integer)
    custom_episode_number: Mapped[int] = mapped_column(Integer)
    absolute_episode_number: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint('tmdb_episode_group_id', 'tmdb_episode_id', name='idx_group_episode_unique'),
        Index('idx_custom_season_episode', 'tmdb_tv_id', 'tmdb_episode_group_id', 'custom_season_number', 'custom_episode_number'),
        Index('idx_absolute_episode', 'tmdb_tv_id', 'tmdb_episode_group_id', 'absolute_episode_number'),
    )

class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    job_type: Mapped[str] = mapped_column(String(50))
    cron_expression: Mapped[str] = mapped_column(String(100))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

class TaskHistory(Base):
    __tablename__ = "task_history"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[Optional[str]] = mapped_column(TEXT)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), default=datetime.now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

    __table_args__ = (Index('idx_created_at', 'created_at', mysql_length={'created_at': None}),)

class ExternalApiLog(Base):
    __tablename__ = "external_api_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    access_time: Mapped[datetime] = mapped_column(TIMESTAMP, server_default=func.now())
    ip_address: Mapped[str] = mapped_column(String(45))
    endpoint: Mapped[str] = mapped_column(String(255))
    status_code: Mapped[int] = mapped_column(Integer)
    message: Mapped[Optional[str]] = mapped_column(TEXT)
