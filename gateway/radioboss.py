"""RadioBOSS Remote Control API client (localhost).

Mirrors the MediaCore PHP adapter: the same command mapping and the same
tolerant TRACK parsing (//TRACK, UPPERCASE attributes with fallbacks), so the
gateway and the server agree on what "confirmed in the playlist" means.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("gateway.radioboss")


class RadioBoss:
    def __init__(
        self,
        base_url: str,
        password: str = "",
        user: str | None = None,
        timeout: float = 5.0,
        insert_position: str = "-2",
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.password = password
        self.user = user or None
        self.timeout = timeout
        self.insert_position = insert_position

    # ── transport ──────────────────────────────────────────────────────
    def _call(self, params: dict, method: str = "GET") -> str | None:
        p = dict(params)
        if self.user:
            p["user"] = self.user
        p["pass"] = self.password
        try:
            if method == "POST":
                r = requests.post(self.base_url, data=p, timeout=self.timeout)
            else:
                r = requests.get(self.base_url, params=p, timeout=self.timeout)
            if r.ok:
                return r.text
            log.warning("RadioBOSS returned HTTP %s for %s", r.status_code, params.get("action") or params.get("cmd"))
        except requests.RequestException as exc:
            log.warning("RadioBOSS request failed: %s", exc)
        return None

    @staticmethod
    def _is_ok(body: str | None) -> bool:
        if body is None:
            return False
        b = body.strip()
        return b.upper().startswith("OK") or "<Info" in b

    # ── write commands (vendor-neutral, matches the ERP outbox) ─────────
    def execute(self, ctype: str, args: dict) -> tuple[bool, str | None]:
        """Run one write command; return (ok, raw_response)."""
        if ctype == "inserttrack":
            params = {
                "action": "inserttrack",
                "filename": args.get("file"),
                "pos": str(args.get("pos") or self.insert_position),
            }
            if args.get("title"):
                params["streamingtitle"] = args["title"]
            params = {k: v for k, v in params.items() if v not in (None, "")}
            if "filename" not in params:
                return (False, "missing filename")
        elif ctype == "delete":
            positions = args.get("positions") or []
            params = {"action": "delete", "pos": ",".join(str(int(p)) for p in positions)}
        elif ctype == "setnexttrack":
            params = {"action": "setnexttrack", "pos": str(args.get("position", 0))}
        elif ctype == "setcasttitle":
            params = {"action": "setcasttitle", "title": args.get("title", "")}
        elif ctype == "command":
            params = {"cmd": args.get("cmd", "")}  # transport control: next / stop / play N
        else:
            return (False, f"unsupported command type: {ctype}")

        body = self._call(params)
        return (self._is_ok(body), body)

    # ── reads ──────────────────────────────────────────────────────────
    def playlist(self) -> list[dict]:
        return self._parse_tracks(self._call({"action": "getplaylist2"}), with_position=True)

    def playback_info(self) -> dict | None:
        body = self._call({"action": "playbackinfo"})
        root = self._xml(body)
        if root is None:
            return None
        pb = root.find("Playback")
        return {
            "state": pb.get("state") if pb is not None else None,
            "position": self._int(pb.get("pos")) if pb is not None else None,          # ms into the track
            "length": self._int(pb.get("len")) if pb is not None else None,            # track length (ms)
            "time_left": self._int(pb.get("playingtimeleft")) if pb is not None else None,
            "current": self._track(self._find_track(root, "CurrentTrack")),
            "next": self._track(self._find_track(root, "NextTrack")),
        }

    @staticmethod
    def _int(v):
        try:
            return int(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None

    def recently_played(self, limit: int = 5) -> list[dict]:
        """Most-recently-aired tracks (RadioBOSS getlastplayed) — feeds the
        console's 'Previous' card for gateway stations (unreachable from cloud)."""
        return self._parse_tracks(self._call({"action": "getlastplayed"}))[: max(1, limit)]

    def play_history(self) -> list[dict] | None:
        """Full available history with source start timestamps; None on failure."""
        root = self._xml(self._call({"action": "getlastplayed"}))
        if root is None or root.tag != "LastPlayed":
            return None
        tracks = []
        for node in root.iter("TRACK"):
            row = self._track(node)
            if row is not None:
                row["start_time"] = self._attr(node, "STARTTIME")
                tracks.append(row)
        return tracks

    def healthy(self) -> bool:
        body = self._call({"action": "playbackinfo"})
        return body is not None and "<Info" in body

    def encoder_status(self) -> list[dict]:
        """Broadcasting encoder status (are we live to listeners?). Reachable only
        on the studio LAN, so it rides the heartbeat for gateway-mode stations —
        the cloud can't call RadioBOSS localhost directly. Attributes lowercased,
        matching the server-side RadioBoss adapter."""
        root = self._xml(self._call({"action": "encoderstatus"}))
        if root is None:
            return []
        nodes = list(root.iter("Encoder")) or list(root.iter("ENCODER"))
        return [{k.lower(): v for k, v in n.attrib.items()} for n in nodes]

    def in_playlist(self, args: dict) -> bool:
        """Whether the creative referenced by an inserttrack payload is queued."""
        return self.count_in_playlist(args) > 0

    def first_position(self, args: dict) -> int | None:
        """Playlist position of the first queued track matching a creative
        payload (by filename or cast title) — used by force_play to cue the
        creative it just inserted as the next track. None if not found."""
        file = (args.get("file") or "").lower()
        title = (args.get("title") or "").lower()
        if not file and not title:
            return None
        for t in self.playlist():
            if (file and t.get("filename", "").lower() == file) or (
                title and t.get("casttitle", "").lower() == title
            ):
                return t.get("position")
        return None

    def count_in_playlist(self, args: dict) -> int:
        """How many playlist entries match the inserttrack payload.

        A count (not a boolean) is what makes confirmation correct for a creative
        that legitimately airs more than once: the same ad booked for two spots
        appears twice, so 'present' can't tell the two inserts apart — but a
        count going from N to N+1 confirms *this* insert specifically.
        """
        file = (args.get("file") or "").lower()
        title = (args.get("title") or "").lower()
        if not file and not title:
            return 0
        n = 0
        for t in self.playlist():
            if (file and t.get("filename", "").lower() == file) or (
                title and t.get("casttitle", "").lower() == title
            ):
                n += 1
        return n

    # ── parsing ────────────────────────────────────────────────────────
    @staticmethod
    def _xml(body: str | None):
        if not body:
            return None
        try:
            return ET.fromstring(body)
        except ET.ParseError:
            return None

    @staticmethod
    def _find_track(root, wrapper: str):
        node = root.find(wrapper)
        return node.find("TRACK") if node is not None else None

    @staticmethod
    def _attr(node, *names: str) -> str:
        for name in names:
            val = node.get(name)
            if val:
                return val
        return ""

    @classmethod
    def _track(cls, node) -> dict | None:
        if node is None:
            return None
        title = cls._attr(node, "TITLE")
        artist = cls._attr(node, "ARTIST")
        filename = cls._attr(node, "FILENAME")
        if not title and not artist and not filename:
            return None
        return {
            "title": title,
            "artist": artist,
            "filename": filename,
            "casttitle": cls._attr(node, "CASTTITLE", "STREAMINGTITLE", "TITLE"),
            "duration": cls._attr(node, "DURATION"),
        }

    @classmethod
    def _parse_tracks(cls, body: str | None, with_position: bool = False) -> list[dict]:
        root = cls._xml(body)
        if root is None:
            return []
        out: list[dict] = []
        for i, node in enumerate(root.iter("TRACK")):
            row = {
                "title": cls._attr(node, "TITLE"),
                "artist": cls._attr(node, "ARTIST"),
                "filename": cls._attr(node, "FILENAME"),
                "casttitle": cls._attr(node, "CASTTITLE", "STREAMINGTITLE", "TITLE"),
                "duration": cls._attr(node, "DURATION"),
            }
            if with_position:
                pos = cls._attr(node, "INDEX", "POS", "POSITION")
                row["position"] = int(pos) if pos.isdigit() else i + 1
            out.append(row)
        return out
