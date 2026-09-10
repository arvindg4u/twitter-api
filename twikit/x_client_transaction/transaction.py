import re
import bs4
import math
import time
import random
import base64
import hashlib
from typing import Union, List
from functools import reduce
from .cubic_curve import Cubic
from .interpolate import interpolate
from .rotation import convert_rotation_to_matrix
from .utils import float_to_hex, is_odd, base64_encode, handle_x_migration

ON_DEMAND_FILE_REGEX = re.compile(
    r',(\d+):["\']ondemand\.s["\']', flags=(re.VERBOSE | re.MULTILINE))
# Current X frontend embeds the webpack chunk map as `59924:"ondemand.s"`
# (quotes around the number are optional) with the content hash in a
# separate `59924:"<hex>"` entry (see https://x.com/home HTML).
ON_DEMAND_FILE_REGEX_NEW = re.compile(r'"?(\d+)"?:"ondemand\.s"')
ON_DEMAND_HASH_PATTERN = r',{}:["\']([0-9a-f]+)["\']'
ON_DEMAND_HASH_PATTERN_NEW = r'"?{0}"?:"([0-9a-f]{{10,}})"'
INDICES_REGEX = re.compile(r'\[(\d+)\],\s*16')


class ClientTransaction:
    ADDITIONAL_RANDOM_NUMBER = 3
    DEFAULT_KEYWORD = "obfiowerehiring"
    DEFAULT_ROW_INDEX = None
    DEFAULT_KEY_BYTES_INDICES = None

    def __init__(self):
        self.home_page_response = None

    async def init(self, session, headers):
        home_page_response = await handle_x_migration(session, headers)

        self.home_page_response = self.validate_response(home_page_response)
        self.DEFAULT_ROW_INDEX, self.DEFAULT_KEY_BYTES_INDICES = await self.get_indices(
            self.home_page_response, session, headers)
        self.key = self.get_key(response=self.home_page_response)
        self.key_bytes = self.get_key_bytes(key=self.key)
        self.animation_key = self.get_animation_key(
            key_bytes=self.key_bytes, response=self.home_page_response)

    async def _fetch_indices_from_js(self, session, headers, file_hash):
        """Download ondemand.s.<hash>a.js and extract KEY_BYTE indices."""
        indices = []
        on_demand_file_url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{file_hash}a.js"
        on_demand_file_response = await session.request(
            method="GET", url=on_demand_file_url, headers=headers)
        for item in INDICES_REGEX.finditer(on_demand_file_response.text):
            indices.append(item.group(1))
        return indices

    async def get_indices(self, home_page_response, session, headers):
        key_byte_indices = []
        response = self.validate_response(
            home_page_response) or self.home_page_response
        page = str(response)
        # Format 1 (older): `,<chunk>:"ondemand.s"` + `,<chunk>:"<hash>"`
        on_demand_match = ON_DEMAND_FILE_REGEX.search(page)
        if on_demand_match:
            chunk_index = on_demand_match.group(1)
            hash_match = re.search(
                ON_DEMAND_HASH_PATTERN.format(chunk_index), page)
            if hash_match:
                key_byte_indices = await self._fetch_indices_from_js(
                    session, headers, hash_match.group(1))
        # Format 2 (current X frontend): `"<chunk>":"ondemand.s"` in the
        # webpack chunk map (see https://x.com/home HTML) with the hash in
        # a separate `"<chunk>":"<hex>"` entry.
        if not key_byte_indices:
            on_demand_match = ON_DEMAND_FILE_REGEX_NEW.search(page)
            if on_demand_match:
                chunk_index = on_demand_match.group(1)
                hash_match = re.search(
                    ON_DEMAND_HASH_PATTERN_NEW.format(chunk_index), page)
                if hash_match:
                    key_byte_indices = await self._fetch_indices_from_js(
                        session, headers, hash_match.group(1))
        # Format 3 (fallback): the logged-out landing page (https://x.com)
        # no longer embeds the chunk map — refetch from /home which does.
        if not key_byte_indices:
            home_response = await session.request(
                method="GET", url="https://x.com/home", headers=headers)
            import bs4 as _bs4
            home_page = _bs4.BeautifulSoup(home_response.content, 'lxml')
            home_str = str(home_page)
            on_demand_match = ON_DEMAND_FILE_REGEX_NEW.search(
                home_str) or ON_DEMAND_FILE_REGEX.search(home_str)
            if on_demand_match:
                chunk_index = on_demand_match.group(1)
                hash_match = re.search(
                    ON_DEMAND_HASH_PATTERN_NEW.format(chunk_index),
                    home_str) or re.search(
                    ON_DEMAND_HASH_PATTERN.format(chunk_index), home_str)
                if hash_match:
                    key_byte_indices = await self._fetch_indices_from_js(
                        session, headers, hash_match.group(1))
                    self.home_page_response = home_page
        if not key_byte_indices:
            raise Exception("Couldn't get KEY_BYTE indices")
        key_byte_indices = list(map(int, key_byte_indices))
        return key_byte_indices[0], key_byte_indices[1:]

    def validate_response(self, response: bs4.BeautifulSoup):
        if not isinstance(response, bs4.BeautifulSoup):
            raise Exception("invalid response")
        return response

    def get_key(self, response=None):
        response = self.validate_response(response) or self.home_page_response
        # <meta name="twitter-site-verification" content="mentU...+1yPz..../IcNS+......./RaF...R+b"/>
        element = response.select_one("[name='twitter-site-verification']")
        if not element:
            raise Exception("Couldn't get key from the page source")
        return element.get("content")

    def get_key_bytes(self, key: str):
        return list(base64.b64decode(bytes(key, 'utf-8')))

    def get_frames(self, response=None):
        # loading-x-anim-0...loading-x-anim-3
        response = self.validate_response(response) or self.home_page_response
        return response.select("[id^='loading-x-anim']")

    def get_2d_array(self, key_bytes: List[Union[float, int]], response, frames: bs4.ResultSet = None):
        if not frames:
            frames = self.get_frames(response)
        # return list(list(frames[key[5] % 4].children)[0].children)[1].get("d")[9:].split("C")
        return [[int(x) for x in re.sub(r"[^\d]+", " ", item).strip().split()] for item in list(list(frames[key_bytes[5] % 4].children)[0].children)[1].get("d")[9:].split("C")]

    def solve(self, value, min_val, max_val, rounding: bool):
        result = value * (max_val-min_val) / 255 + min_val
        return math.floor(result) if rounding else round(result, 2)

    def animate(self, frames, target_time):
        # from_color = f"#{''.join(['{:x}'.format(digit) for digit in frames[:3]])}"
        # to_color = f"#{''.join(['{:x}'.format(digit) for digit in frames[3:6]])}"
        # from_rotation = "rotate(0deg)"
        # to_rotation = f"rotate({solve(frames[6], 60, 360, True)}deg)"
        # easing_values = [solve(value, -1 if count % 2 else 0, 1, False)
        #                  for count, value in enumerate(frames[7:])]
        # easing = f"cubic-bezier({','.join([str(value) for value in easing_values])})"
        # current_time = round(target_time / 10) * 10

        from_color = [float(item) for item in [*frames[:3], 1]]
        to_color = [float(item) for item in [*frames[3:6], 1]]
        from_rotation = [0.0]
        to_rotation = [self.solve(float(frames[6]), 60.0, 360.0, True)]
        frames = frames[7:]
        curves = [self.solve(float(item), is_odd(counter), 1.0, False)
                  for counter, item in enumerate(frames)]
        cubic = Cubic(curves)
        val = cubic.get_value(target_time)
        color = interpolate(from_color, to_color, val)
        color = [value if value > 0 else 0 for value in color]
        rotation = interpolate(from_rotation, to_rotation, val)
        matrix = convert_rotation_to_matrix(rotation[0])
        # str_arr = [format(int(round(color[i])), '02x') for i in range(len(color) - 1)]
        # str_arr = [format(int(round(color[i])), 'x') for i in range(len(color) - 1)]
        str_arr = [format(round(value), 'x') for value in color[:-1]]
        for value in matrix:
            rounded = round(value, 2)
            if rounded < 0:
                rounded = -rounded
            hex_value = float_to_hex(rounded)
            str_arr.append(f"0{hex_value}".lower() if hex_value.startswith(
                ".") else hex_value if hex_value else '0')
        str_arr.extend(["0", "0"])
        animation_key = re.sub(r"[.-]", "", "".join(str_arr))
        return animation_key

    def get_animation_key(self, key_bytes, response):
        total_time = 4096
        # row_index, frame_time = [key_bytes[2] % 16, key_bytes[12] % 16 * (key_bytes[14] % 16) * (key_bytes[7] % 16)]
        # row_index, frame_time = [key_bytes[2] % 16, key_bytes[2] % 16 * (key_bytes[42] % 16) * (key_bytes[45] % 16)]

        row_index = key_bytes[self.DEFAULT_ROW_INDEX] % 16
        frame_time = reduce(lambda num1, num2: num1*num2,
                            [key_bytes[index] % 16 for index in self.DEFAULT_KEY_BYTES_INDICES])
        arr = self.get_2d_array(key_bytes, response)
        frame_row = arr[row_index]

        target_time = float(frame_time) / total_time
        animation_key = self.animate(frame_row, target_time)
        return animation_key

    def generate_transaction_id(self, method: str, path: str, response=None, key=None, animation_key=None, time_now=None):
        time_now = time_now or math.floor(
            (time.time() * 1000 - 1682924400 * 1000) / 1000)
        time_now_bytes = [(time_now >> (i * 8)) & 0xFF for i in range(4)]
        key = key or self.key or self.get_key(response)
        key_bytes = self.get_key_bytes(key)
        animation_key = animation_key or self.animation_key or self.get_animation_key(
            key_bytes, response)
        # hash_val = hashlib.sha256(f"{method}!{path}!{time_now}bird{animation_key}".encode()).digest()
        hash_val = hashlib.sha256(
            f"{method}!{path}!{time_now}{self.DEFAULT_KEYWORD}{animation_key}".encode()).digest()
        # hash_bytes = [int(hash_val[i]) for i in range(len(hash_val))]
        hash_bytes = list(hash_val)
        random_num = random.randint(0, 255)
        bytes_arr = [*key_bytes, *time_now_bytes, *
                     hash_bytes[:16], self.ADDITIONAL_RANDOM_NUMBER]
        out = bytearray(
            [random_num, *[item ^ random_num for item in bytes_arr]])
        return base64_encode(out).strip("=")
