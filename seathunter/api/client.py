"""HTTP API client for seat booking interactions.

Extracted from killer.py:324-422.
"""

from __future__ import annotations

import logging
import math
import threading
from copy import copy
from email.utils import parsedate_to_datetime
from time import sleep, monotonic
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any
from urllib.parse import unquote

import requests

from seathunter.api.token import generate_booking_data
from seathunter.auth.session_manager import SessionManager

logger = logging.getLogger("seathunter.api")


def retry_after_seconds(value):
    """Parse the server's Retry-After header, accepting seconds or an HTTP date."""
    if value is None:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(str(value))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delay = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None


class ApiClient:
    """Handles all HTTP interactions with the library booking API."""

    def __init__(self, session_manager: SessionManager):
        self.session_mgr = session_manager
        self._session = None

    @property
    def session(self) -> requests.Session:
        return self._session if self._session is not None else self.session_mgr.session

    def fork(self):
        """Give a worker its own cookies, headers and reusable connection pool."""
        client = ApiClient(self.session_mgr)
        source = self.session
        session = requests.Session()
        session.headers = source.headers.copy()
        session.headers.pop("Api-Token", None)
        session.headers.pop("Content-Length", None)
        session.cookies = source.cookies.copy()
        session.params = copy(source.params)
        session.proxies = source.proxies.copy()
        session.auth = source.auth
        session.trust_env = source.trust_env
        session.verify = source.verify
        session.cert = source.cert
        client._session = session
        return client

    def close(self):
        if self._session is not None:
            self._session.close()

    @property
    def base_url(self) -> str:
        return self.session_mgr.base_url

    def query_rooms(self) -> Dict[str, Any]:
        """Query all available room types and their data.

        Returns dict mapping room name -> room data dict.
        """
        url = self.base_url + "/Space/Category/list"
        self.session.cookies.update({"org_id": "104"})
        resp = self.session.get(url=url, timeout=30)
        resp.raise_for_status()
        result = resp.json()

        raw_rooms = result["content"]["children"][1]["defaultItems"]
        rooms = {}
        for item in raw_rooms:
            room_name = item["name"]
            query_str = unquote(item["link"]["url"]).split("?")[1]
            room_resp = self.session.get(
                url=self.base_url + "/Seat/Index/searchSeats?" + query_str,
                timeout=30,
            ).json()
            rooms[room_name] = room_resp["data"]
            sleep(2)  # Rate limiting
        return rooms

    def query_seats(self, rooms: Dict[str, Any]) -> Dict[str, Any]:
        """Query seat information for each room's floors.

        Mutates rooms dict in-place, adding 'floors' key to each room.
        """
        now = datetime.now()
        if now.hour >= 22:
            now = (now + timedelta(days=1)).replace(hour=11, minute=0, second=0)

        for room_name, room_data in rooms.items():
            data = {
                "beginTime": now.timestamp(),
                "duration": 3600,
                "num": 1,
                "space_category[category_id]": room_data["space_category"]["category_id"],
                "space_category[content_id]": room_data["space_category"]["content_id"],
            }
            resp = self.session.post(
                url=self.base_url + "/Seat/Index/searchSeats",
                data=data,
                timeout=30,
            ).json()
            room_data["floors"] = {
                x["roomName"]: x
                for x in resp["allContent"]["children"][2]["children"]["children"]
            }
            for floor_data in room_data["floors"].values():
                floor_data["seats"] = floor_data["seatMap"]["POIs"]
            sleep(2)  # Rate limiting

        return rooms

    def book_seat(self, begin_time: datetime, duration_hours: int,
                  seat_ids: List[str], booker_uids: List[str], *, before_send=None,
                  can_send=None) -> Dict:
        """Execute a single booking attempt.

        Returns the raw API response dict.
        """
        if before_send is not None and not before_send():
            return {"CODE": "not_sent", "MESSAGE": "Stopped before POST"}
        # A limiter may wait for many seconds. Sign only after admission, so
        # api_time describes this attempt instead of an expired queue entry.
        data, api_token = generate_booking_data(
            begin_time, duration_hours, seat_ids, booker_uids
        )
        url = self.base_url + "/Seat/Index/bookSeats"
        # Tokens belong to one payload, never to shared mutable session headers.
        # Requests calculates Content-Length from the actual encoded body.
        headers = {"Api-Token": api_token, "Content-Length": None}
        if can_send is not None and not can_send():
            return {"CODE": "not_sent", "MESSAGE": "Stopped during request preparation"}
        started_at = datetime.now()
        started_tick = monotonic()
        status = None
        logger.info("Booking POST dispatch: %s; worker=%s",
                    started_at.isoformat(timespec="milliseconds"), threading.current_thread().name)
        try:
            # Redirects can silently resubmit a POST and bypass the global gate.
            resp = self.session.post(url=url, data=data, headers=headers,
                                     timeout=(3.05, 30), allow_redirects=False)
            status = resp.status_code
            delay = retry_after_seconds(resp.headers.get("Retry-After"))
            try:
                result = resp.json()
            except ValueError:
                result = {}
            if not isinstance(result, dict):
                result = {}
            if status == 429:
                result = {"CODE": "429", "MESSAGE": "Too Many Requests"}
            elif not 200 <= status < 300:
                result = {"CODE": str(status), "MESSAGE": f"Booking HTTP status {status}"}
            elif not result:
                result = {"CODE": "error", "MESSAGE": "Booking response was not a JSON object"}
            if delay is not None:
                result["_retry_after_seconds"] = delay
            return result
        except Exception as e:
            logger.error("Booking request failed: %s", e)
            return {"CODE": "error", "MESSAGE": str(e)}
        finally:
            logger.info(
                "Booking HTTP call started at %s; elapsed %.1fms; HTTP status=%s; worker=%s",
                started_at.isoformat(timespec="milliseconds"),
                (monotonic() - started_tick) * 1000,
                status,
                threading.current_thread().name,
            )

    def get_floor_names(self, rooms: Dict, room_name: str) -> List[str]:
        """Get floor list for a room."""
        return list(rooms[room_name]["floors"].keys())

    def get_seats_by_room_and_floor(self, rooms: Dict, room_name: str,
                                     floor_name: str) -> List[Dict]:
        """Get seats for a specific room and floor."""
        return rooms[room_name]["floors"][floor_name]["seats"]
