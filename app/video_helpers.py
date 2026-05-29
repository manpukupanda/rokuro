import re
import sqlite3
from contextlib import closing
from typing import Any

from database import db_connect, now_iso


def parse_tags(tags_str: str) -> list[str]:
    tokens = re.split(r"[\s\u3000]+", tags_str.strip())
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        name = t.lstrip("#").lower()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def set_video_tags(conn: sqlite3.Connection, video_id: str, tags_str: str) -> None:
    names = parse_tags(tags_str)
    conn.execute("DELETE FROM video_tags WHERE video_id = ?", (video_id,))
    ts = now_iso()
    for name in names:
        conn.execute("INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)", (name, ts))
        tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO video_tags (video_id, tag_id) VALUES (?, ?)",
            (video_id, tag_row["id"]),
        )
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM video_tags)")


def load_video_metadata(
    conn: sqlite3.Connection, video_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not video_ids:
        return {}
    placeholders = ",".join("?" for _ in video_ids)
    rows = conn.execute(
        f"""
        SELECT video_id, description, display_order
          FROM video_metadata
         WHERE video_id IN ({placeholders})
        """,
        tuple(video_ids),
    ).fetchall()
    return {
        row["video_id"]: {
            "description": row["description"],
            "display_order": row["display_order"],
        }
        for row in rows
    }


def load_series_orders(
    conn: sqlite3.Connection, video_ids: list[str]
) -> dict[str, int | None]:
    if not video_ids:
        return {}
    placeholders = ",".join("?" for _ in video_ids)
    rows = conn.execute(
        f"""
        SELECT video_id, series_order
          FROM video_series
         WHERE video_id IN ({placeholders})
        """,
        tuple(video_ids),
    ).fetchall()
    return {row["video_id"]: row["series_order"] for row in rows}


def apply_local_video_metadata(videos: list[dict[str, Any]]) -> None:
    if not videos:
        return
    video_ids = [v["public_id"] for v in videos if v.get("public_id")]
    with closing(db_connect()) as conn:
        meta_map = load_video_metadata(conn, video_ids)
        series_order_map = load_series_orders(conn, video_ids)
    for video in videos:
        public_id = video.get("public_id", "")
        meta = meta_map.get(public_id)
        if meta:
            video["description"] = meta["description"]
            video["display_order"] = meta["display_order"]
        else:
            video["description"] = ""
            video["display_order"] = 0
        video["series_order"] = series_order_map.get(public_id)


def sort_videos(videos: list[dict[str, Any]], *, prefer_series_order: bool = False) -> list[dict[str, Any]]:
    max_order = 2147483647
    if prefer_series_order:
        return sorted(
            videos,
            key=lambda v: (
                v.get("series_order") if v.get("series_order") is not None else max_order,
                int(v.get("display_order", 0)),
                v.get("created_at", ""),
                v.get("public_id", ""),
            ),
        )
    return sorted(
        videos,
        key=lambda v: (
            int(v.get("display_order", 0)),
            v.get("created_at", ""),
            v.get("public_id", ""),
        ),
    )


def load_classification_for_videos(
    videos: list[dict[str, Any]],
) -> tuple[list[dict], list[dict], list[dict]]:
    if not videos:
        return [], [], []

    video_lookup = {v["public_id"]: v for v in videos}
    visible_ids = set(video_lookup.keys())

    with closing(db_connect()) as conn:
        cat_rows = conn.execute(
            """
            SELECT vc.category_id, vc.video_id, c.name
              FROM video_categories vc
              JOIN categories c ON c.id = vc.category_id
             ORDER BY c.display_order, c.name
            """
        ).fetchall()
        cat_map: dict[int, dict] = {}
        for row in cat_rows:
            if row["video_id"] not in visible_ids:
                continue
            cid = row["category_id"]
            if cid not in cat_map:
                cat_map[cid] = {"name": row["name"], "videos": []}
            cat_map[cid]["videos"].append(video_lookup[row["video_id"]])
        for cat in cat_map.values():
            cat["videos"] = sort_videos(cat["videos"])
        categories_with_videos = [v for v in cat_map.values() if v["videos"]]

        series_rows = conn.execute(
            """
            SELECT vs.series_id, vs.video_id, vs.series_order, s.name
              FROM video_series vs
              JOIN series s ON s.id = vs.series_id
             ORDER BY s.display_order, s.name,
                      COALESCE(vs.series_order, 2147483647), vs.created_at
            """
        ).fetchall()
        series_map: dict[int, dict] = {}
        for row in series_rows:
            if row["video_id"] not in visible_ids:
                continue
            sid = row["series_id"]
            if sid not in series_map:
                series_map[sid] = {"name": row["name"], "videos": []}
            video = dict(video_lookup[row["video_id"]])
            video["series_order"] = row["series_order"]
            series_map[sid]["videos"].append(video)
        for series in series_map.values():
            series["videos"] = sort_videos(series["videos"], prefer_series_order=True)
        series_with_videos = [v for v in series_map.values() if v["videos"]]

        tag_rows = conn.execute(
            """
            SELECT vt.tag_id, vt.video_id, t.name
              FROM video_tags vt
              JOIN tags t ON t.id = vt.tag_id
             ORDER BY t.name
            """
        ).fetchall()
        tag_map: dict[int, dict] = {}
        for row in tag_rows:
            if row["video_id"] not in visible_ids:
                continue
            tid = row["tag_id"]
            if tid not in tag_map:
                tag_map[tid] = {"name": row["name"], "videos": []}
            tag_map[tid]["videos"].append(video_lookup[row["video_id"]])
        for tag in tag_map.values():
            tag["videos"] = sort_videos(tag["videos"])
        tags_with_videos = [v for v in tag_map.values() if v["videos"]]

    return categories_with_videos, series_with_videos, tags_with_videos
