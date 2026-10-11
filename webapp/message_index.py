"""In-memory browse indexes owned by one immutable parsed chat.

Only positions are retained in query caches; no persistent copy of chat text.
"""

from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict, defaultdict
from threading import RLock

from analyzer.local_stats import is_session_start


MAX_QUERY_POSITIONS = 200_000
MAX_QUERIES = 16
MAX_NORMALIZED_CHARS = 8_000_000
MAX_TIMELINE_QUERIES = 6
MAX_TIMELINE_SEGMENTS = 120
_BUILD_LOCK = RLock()


class MessageIndex:
    def __init__(self, chat, body, matcher):
        self.chat = chat
        self.body = body
        self.matcher = matcher
        self.ids = defaultdict(list)
        self.normalized = []
        normalized_chars = 0
        for pos, message in enumerate(chat.messages):
            self.ids[message.id].append(pos)
            hay = self._hay(message)
            normalized_chars += len(hay)
            self.normalized.append(hay if normalized_chars <= MAX_NORMALIZED_CHARS else None)
        self.times = sorted((m.time_str, i) for i, m in enumerate(chat.messages))
        self.time_keys = [time for time, _ in self.times]
        self.days = sorted((m.time_str[:10], i) for i, m in enumerate(chat.messages))
        self.day_keys = [day for day, _ in self.days]
        self.queries = OrderedDict()
        self.cached_positions = 0
        self.timelines = OrderedDict()
        self.lock = RLock()

    def _hay(self, message):
        return " ".join(
            x
            for x in (
                self.body(message),
                message.media_label,
                " ".join(message.face_names),
                message.sender_name,
            )
            if x
        ).lower()

    def query(self, q="", side="", month="", dt_from="", dt_to=""):
        key = (q.lower(), side, month, dt_from, dt_to)
        with self.lock:
            if key in self.queries:
                self.queries.move_to_end(key)
                return self.queries[key]
        if not any(key):
            return range(len(self.chat.messages))
        candidates = range(len(self.chat.messages))
        if month:
            lo = bisect_left(self.time_keys, month)
            hi = bisect_right(self.time_keys, month + "\uffff")
            candidates = sorted(pos for _, pos in self.times[lo:hi])
        if dt_from or dt_to:
            lo = bisect_left(self.day_keys, dt_from) if dt_from else 0
            hi = bisect_right(self.day_keys, dt_to) if dt_to else len(self.days)
            dates = sorted(pos for _, pos in self.days[lo:hi])
            if month:
                allowed = set(dates)
                candidates = [pos for pos in candidates if pos in allowed]
            else:
                candidates = dates
        positions = array("I")
        for pos in candidates:
            message = self.chat.messages[pos]
            # Dates are already narrowed by binary lookup; keep legacy sender semantics.
            if side and not self.matcher(message, "", side, "", "", "", self.chat.self_uid):
                continue
            if q and q.lower() not in (self.normalized[pos] or self._hay(message)):
                continue
            positions.append(pos)
        with self.lock:
            if key not in self.queries and len(positions) <= MAX_QUERY_POSITIONS:
                while self.queries and (
                    len(self.queries) >= MAX_QUERIES
                    or self.cached_positions + len(positions) > MAX_QUERY_POSITIONS
                ):
                    _, removed = self.queries.popitem(last=False)
                    self.cached_positions -= len(removed)
                self.queries[key] = positions
                self.cached_positions += len(positions)
        return positions

    def match_quotes(self, quotes):
        """Match all distinct partial quotes in one pass, preserving ambiguity.

        Aho-Corasick keeps a report with many citations from scanning every
        message once per field. Original text is compared without fuzzy edits.
        """
        quotes = list(dict.fromkeys(q for q in quotes if q))
        if not quotes:
            return {}
        # A quote that is a whole message can also occur within another message.
        # Search all patterns, otherwise duplicate/partial sources become falsely unique.
        children, fail, outputs = [{}], [0], [[]]
        for quote in quotes:
            node = 0
            for char in quote:
                if char not in children[node]:
                    children[node][char] = len(children)
                    children.append({})
                    fail.append(0)
                    outputs.append([])
                node = children[node][char]
            outputs[node].append(quote)
        from collections import deque

        queue = deque(children[0].values())
        while queue:
            node = queue.popleft()
            for char, child in children[node].items():
                queue.append(child)
                parent = fail[node]
                while parent and char not in children[parent]:
                    parent = fail[parent]
                fail[child] = children[parent].get(char, 0)
                outputs[child].extend(outputs[fail[child]])
        found = {q: [] for q in quotes}
        for pos, message in enumerate(self.chat.messages):
            node, seen = 0, set()
            for char in self.body(message):
                while node and char not in children[node]:
                    node = fail[node]
                node = children[node].get(char, 0)
                seen.update(outputs[node])
            for quote in seen:
                found[quote].append(pos)
        return found

    def timeline(self, q="", side="", month="", dt_from="", dt_to=""):
        """Aggregate the same indexed matches used by message search.

        Only bounded buckets and recent segment summaries are retained; message text
        is never copied into this cache.
        """
        q = (q or "").strip()
        if not q:
            raise ValueError("请输入关键词后再生成时间线")
        key = (q.lower(), side, month, dt_from, dt_to)
        with self.lock:
            cached = self.timelines.get(key)
            if cached is not None:
                self.timelines.move_to_end(key)
                return cached

        from collections import Counter

        positions = self.query(q, side=side, month=month, dt_from=dt_from, dt_to=dt_to)
        daily, monthly = Counter(), Counter()
        sources = Counter()
        segments = []
        total_segments = 0
        literal_occurrences = 0
        first_time = last_time = ""
        last_ts = None
        needle = q.lower()

        for pos in positions:
            message = self.chat.messages[pos]
            day = message.time_str[:10]
            month = message.time_str[:7]
            daily[day] += 1
            monthly[month] += 1
            if not first_time:
                first_time = message.time_str
            last_time = message.time_str

            fields = []
            if message.text:
                fields.append((message.text, "正文"))
                if message.media_label:
                    fields.append((message.media_label, "媒体标签"))
                if message.face_names:
                    fields.append((" ".join(message.face_names), "表情名"))
            else:
                # 空正文时，浏览器显示的媒体/表情占位是实际可搜索内容；
                # 其中可能已经含 media_label 和 face_names，所以只计一次。
                placeholder = self.body(message)
                if placeholder:
                    fields.append((placeholder, "媒体或表情占位"))
            if message.sender_name:
                fields.append((message.sender_name, "发言人名称"))
            for value, source in fields:
                count = value.lower().count(needle)
                if count:
                    sources[source] += count
                    literal_occurrences += count

            if is_session_start(last_ts, message.timestamp):
                total_segments += 1
                segments.append(
                    {
                        "start": message.time_str,
                        "end": message.time_str,
                        "start_ts": message.timestamp,
                        "end_ts": message.timestamp,
                        "first_date": day,
                        "last_date": day,
                        "count": 0,
                    }
                )
                if len(segments) > MAX_TIMELINE_SEGMENTS:
                    segments.pop(0)
            segment = segments[-1]
            segment["end"] = message.time_str
            segment["end_ts"] = message.timestamp
            segment["last_date"] = day
            segment["count"] += 1
            last_ts = message.timestamp

        result = {
            "query": q,
            "matched_messages": len(positions),
            "literal_occurrences": literal_occurrences,
            "source_counts": dict(sources),
            "first_match": first_time,
            "last_match": last_time,
            "daily": [{"date": day, "count": daily[day]} for day in sorted(daily)],
            "monthly": [{"month": month, "count": monthly[month]} for month in sorted(monthly)],
            "total_segments": total_segments,
            "segments": segments,
            "segments_truncated": max(0, total_segments - len(segments)),
        }
        with self.lock:
            if key not in self.timelines:
                while len(self.timelines) >= MAX_TIMELINE_QUERIES:
                    self.timelines.popitem(last=False)
                self.timelines[key] = result
        return result


def index_for(chat, body, matcher):
    with _BUILD_LOCK:
        if chat._message_index is None:
            chat._message_index = MessageIndex(chat, body, matcher)
        return chat._message_index
