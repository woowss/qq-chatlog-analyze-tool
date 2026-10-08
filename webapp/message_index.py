"""In-memory browse indexes owned by one immutable parsed chat.

Only positions are retained in query caches; no persistent copy of chat text.
"""

from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict, defaultdict
from threading import RLock


MAX_QUERY_POSITIONS = 200_000
MAX_QUERIES = 16
MAX_NORMALIZED_CHARS = 8_000_000
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


def index_for(chat, body, matcher):
    with _BUILD_LOCK:
        if chat._message_index is None:
            chat._message_index = MessageIndex(chat, body, matcher)
        return chat._message_index
