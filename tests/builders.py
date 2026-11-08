# -*- coding: utf-8 -*-
# Copyright 2026 rkttu@rkttu.com (남정현)
# SPDX-License-Identifier: Apache-2.0
"""Synthetic HWP 5.x / HWPX document builders for unit tests (stdlib only).

These writers produce small but structurally valid files so the reader can be
tested without the third-party corpus.  They intentionally implement only the
parts of each format the reader consumes.
"""
import io
import struct
import zipfile
import zlib

# --------------------------------------------------------------------------
# Compound File Binary writer (version 3, 512-byte sectors, with mini stream)
# --------------------------------------------------------------------------

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF
SS = 512
MSS = 64
CUTOFF = 4096


def build_cfb(streams):
    """streams: dict 'Storage/Name' -> bytes.  Returns the compound file bytes."""
    # ---- directory tree -------------------------------------------------
    entries = [{"name": "Root Entry", "type": 5, "children": [], "data": None}]
    index = {"": 0}
    for path in sorted(streams):
        parts = path.split("/")
        parent = ""
        for depth, part in enumerate(parts):
            cur = (parent + "/" + part) if parent else part
            if cur not in index:
                is_stream = depth == len(parts) - 1
                entries.append({"name": part, "type": 2 if is_stream else 1,
                                "children": [], "data": streams[path] if is_stream else None})
                index[cur] = len(entries) - 1
                entries[index[parent]]["children"].append(index[cur])
            parent = cur

    # ---- mini stream -------------------------------------------------------
    mini = bytearray()
    minifat = []
    for e in entries:
        e["start"], e["size"] = ENDOFCHAIN, 0
        if e["type"] != 2:
            continue
        data = e["data"]
        e["size"] = len(data)
        if 0 < len(data) < CUTOFF:
            first = len(mini) // MSS
            nsec = (len(data) + MSS - 1) // MSS
            for k in range(nsec):
                minifat.append(first + k + 1 if k < nsec - 1 else ENDOFCHAIN)
            mini += data + b"\0" * (nsec * MSS - len(data))
            e["start"] = first

    # ---- regular sector allocation -------------------------------------------
    sectors = []          # list of bytes (each SS long)
    fat = []

    def alloc(data):
        if not data:
            return ENDOFCHAIN
        n = (len(data) + SS - 1) // SS
        first = len(sectors)
        padded = data + b"\0" * (n * SS - len(data))
        for k in range(n):
            sectors.append(padded[k * SS:(k + 1) * SS])
            fat.append(first + k + 1 if k < n - 1 else ENDOFCHAIN)
        return first

    for e in entries:
        if e["type"] == 2 and e["size"] >= CUTOFF:
            e["start"] = alloc(e["data"])
    root = entries[0]
    root["start"] = alloc(bytes(mini)) if mini else ENDOFCHAIN
    root["size"] = len(mini)
    minifat_bytes = struct.pack("<%dI" % len(minifat), *minifat) if minifat else b""
    first_minifat = alloc(minifat_bytes) if minifat else ENDOFCHAIN
    n_minifat = (len(minifat_bytes) + SS - 1) // SS

    # directory entries: siblings chained through right pointers
    for e in entries:
        e["left"] = e["right"] = e["child"] = NOSTREAM
    for e in entries:
        kids = e["children"]
        if kids:
            e["child"] = kids[0]
            for a, b in zip(kids, kids[1:]):
                entries[a]["right"] = b
    dir_bytes = bytearray()
    for e in entries:
        name = e["name"].encode("utf-16-le") + b"\0\0"
        ent = bytearray(128)
        ent[:len(name)] = name
        struct.pack_into("<HBB", ent, 64, len(name), e["type"], 1)
        struct.pack_into("<3I", ent, 68, e["left"], e["right"], e["child"])
        struct.pack_into("<IQ", ent, 116, e["start"], e["size"])
        dir_bytes += ent
    while len(dir_bytes) % SS:
        # unused directory slots: type 0 (unknown) with NOSTREAM links
        ent = bytearray(128)
        struct.pack_into("<3I", ent, 68, NOSTREAM, NOSTREAM, NOSTREAM)
        dir_bytes += ent
    first_dir = alloc(bytes(dir_bytes))

    # FAT sectors (account for themselves)
    n_fat = 1
    while True:
        total = len(sectors) + n_fat
        if (total + 127) // 128 <= n_fat:
            break
        n_fat += 1
    fat_first = len(sectors)
    for _ in range(n_fat):
        fat.append(FATSECT)
    fat += [FREESECT] * (n_fat * 128 - len(fat))
    fat_bytes = struct.pack("<%dI" % len(fat), *fat)
    for k in range(n_fat):
        sectors.append(fat_bytes[k * SS:(k + 1) * SS])
    assert n_fat <= 109, "test builder supports up to 109 FAT sectors"

    header = bytearray(SS)
    header[:8] = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
    struct.pack_into("<HHHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 0x2C, n_fat)
    struct.pack_into("<I", header, 0x30, first_dir)
    struct.pack_into("<I", header, 0x38, CUTOFF)
    struct.pack_into("<I", header, 0x3C, first_minifat)
    struct.pack_into("<I", header, 0x40, n_minifat)
    struct.pack_into("<I", header, 0x44, ENDOFCHAIN)
    struct.pack_into("<I", header, 0x48, 0)
    difat = [fat_first + k for k in range(n_fat)] + [FREESECT] * (109 - n_fat)
    struct.pack_into("<109I", header, 0x4C, *difat)
    return bytes(header) + b"".join(sectors)


# --------------------------------------------------------------------------
# HWP 5.x record helpers
# --------------------------------------------------------------------------

def rec(tag, level, payload=b""):
    size = len(payload)
    if size >= 0xFFF:
        return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), size) + payload
    return struct.pack("<I", tag | (level << 10) | (size << 20)) + payload


def cid(s):
    """4-char control id as packed by HWP (big-endian chars in a LE UINT32)."""
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])


def wstr(s):
    return struct.pack("<H", len(s.encode("utf-16-le")) // 2) + s.encode("utf-16-le")


def para_text(pieces):
    """pieces: str | ('ext', code, 'ctid') | ('inline', code) | ('char', code)."""
    out = bytearray()
    for p in pieces:
        if isinstance(p, str):
            out += p.encode("utf-16-le")
        elif p[0] == "ext":
            code, ident = p[1], cid(p[2])
            out += struct.pack("<HI", code, ident) + b"\0" * 8 + struct.pack("<H", code)
        elif p[0] == "inline":
            code = p[1]
            out += struct.pack("<H", code) + b"\0" * 12 + struct.pack("<H", code)
        elif p[0] == "char":
            out += struct.pack("<H", p[1])
    out += struct.pack("<H", 13)
    return bytes(out)


def para_header(nchars, shape_id=0, style_id=0, brk=0):
    return struct.pack("<IIHBBHHHI", nchars, 0, shape_id, style_id, brk, 1, 0, 1, 0)


def paragraph(level, pieces, ctrl_records=(), shape_id=0, style_id=0):
    """Return records of one paragraph; ctrl_records are byte blobs at level+1."""
    txt = para_text(pieces) if pieces is not None else b""
    out = rec(0x42, level, para_header(len(txt) // 2, shape_id, style_id))
    if pieces is not None:
        out += rec(0x43, level + 1, txt)
    for c in ctrl_records:
        out += c
    return out


def list_header_cell(level, col, row, colspan=1, rowspan=1, nparas=1):
    payload = struct.pack("<HHI", nparas, 0, 0)
    payload += struct.pack("<HHHH", col, row, colspan, rowspan)
    payload += struct.pack("<II", 1000, 1000) + struct.pack("<4H", 0, 0, 0, 0) + struct.pack("<H", 1)
    return rec(0x48, level, payload)


def table_ctrl(level, rows, cols, cells, caption=None):
    """cells: list of (row, col, rowspan, colspan, [pieces...] or records-bytes)."""
    common = struct.pack("<I", cid("tbl ")) + struct.pack("<IiiIIiHHHHII", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    out = rec(0x47, level, common)
    if caption is not None:
        out += rec(0x48, level + 1, struct.pack("<HHI", 1, 0, 0) + struct.pack("<IIII", 0, 0, 0, 0))
        out += paragraph(level + 1, caption)
    row_sizes = [sum(1 for c in cells if c[0] == r) for r in range(rows)]
    tbl = struct.pack("<IHHH", 0, rows, cols, 0) + struct.pack("<4H", 0, 0, 0, 0)
    tbl += struct.pack("<%dH" % rows, *row_sizes) + struct.pack("<H", 1)
    out += rec(0x4D, level + 1, tbl)
    for (r, c, rs, cs, content) in cells:
        out += list_header_cell(level + 1, c, r, cs, rs)
        if isinstance(content, bytes):
            out += content
        else:
            out += paragraph(level + 1, content)
    return out


def ctrl_with_list(level, ident, paras, extra=b""):
    out = rec(0x47, level, struct.pack("<I", cid(ident)) + extra)
    out += rec(0x48, level + 1, struct.pack("<HHI", len(paras), 0, 0))
    for pieces in paras:
        out += paragraph(level + 1, pieces)
    return out


def hyperlink_ctrl(level, url):
    cmd = url.replace(":", "\\:") + ";1;0;0;"
    payload = struct.pack("<I", cid("%hlk")) + struct.pack("<IB", 0, 0) + wstr(cmd) + struct.pack("<I", 7)
    return rec(0x47, level, payload)


def equation_ctrl(level, script):
    out = rec(0x47, level, struct.pack("<I", cid("eqed")) + b"\0" * 40)
    out += rec(0x58, level + 1, struct.pack("<I", 0) + wstr(script) + b"\0" * 12)
    return out


def picture_ctrl(level, bin_id, description=""):
    common = struct.pack("<I", cid("gso ")) + struct.pack("<IiiIIi", 0, 0, 0, 0, 0, 0)
    common += struct.pack("<4H", 0, 0, 0, 0) + struct.pack("<Ii", 0, 0) + wstr(description)
    out = rec(0x47, level, common)
    out += rec(0x4C, level + 1, b"\0" * 40)
    pic = bytearray(80)
    struct.pack_into("<H", pic, 71, bin_id)
    out += rec(0x55, level + 2, bytes(pic))
    return out


def textbox_ctrl(level, paras):
    common = struct.pack("<I", cid("gso ")) + b"\0" * 40
    out = rec(0x47, level, common)
    out += rec(0x4C, level + 1, b"\0" * 40)
    out += rec(0x48, level + 2, struct.pack("<HHI", len(paras), 0, 0) + b"\0" * 16)
    for pieces in paras:
        out += paragraph(level + 2, pieces)
    out += rec(0x4F, level + 2, b"\0" * 20)
    return out


def para_shape(head_type=0, level=0, num_id=0):
    prop1 = (head_type & 3) << 23 | (level & 7) << 25
    data = bytearray(54)
    struct.pack_into("<I", data, 0, prop1)
    struct.pack_into("<H", data, 30, num_id)
    return bytes(data)


def numbering_record(templates, fmt_codes, start=1):
    out = bytearray()
    for i in range(7):
        prop = (fmt_codes[i] & 0x0F) << 5
        out += struct.pack("<IHHI", prop, 0, 0, 0) + wstr(templates[i])
    out += struct.pack("<H", start)
    out += struct.pack("<7I", *([start] * 7))
    return bytes(out)


def style_record(name, eng=""):
    return wstr(name) + wstr(eng) + struct.pack("<BBhHH", 0, 0, 0x412, 0, 0)


def file_header(compressed=True, password=False, distribution=False, version=(5, 1, 0, 1)):
    flags = (1 if compressed else 0) | (2 if password else 0) | (4 if distribution else 0)
    v = (version[0] << 24) | (version[1] << 16) | (version[2] << 8) | version[3]
    fh = bytearray(256)
    sig = b"HWP Document File"
    fh[:len(sig)] = sig
    struct.pack_into("<II", fh, 32, v, flags)
    return bytes(fh)


def deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def build_hwp(docinfo, sections, compressed=True, password=False, extra_streams=None,
              summary=None, prvtext=None, version=(5, 1, 0, 1)):
    streams = {"FileHeader": file_header(compressed, password, version=version)}
    streams["DocInfo"] = deflate(docinfo) if compressed else docinfo
    for i, s in enumerate(sections):
        streams["BodyText/Section%d" % i] = deflate(s) if compressed else s
    if prvtext is not None:
        streams["PrvText"] = prvtext.encode("utf-16-le")
    if summary is not None:
        streams["\x05HwpSummaryInformation"] = summary
    if extra_streams:
        streams.update(extra_streams)
    return build_cfb(streams)


def summary_info(props):
    """Minimal OLE property set with VT_LPWSTR string properties {pid: str}."""
    body = bytearray()
    entries = []
    offset = 8 + 8 * len(props)
    for pid, value in props.items():
        raw = (value + "\0").encode("utf-16-le")
        blob = struct.pack("<HHI", 0x1F, 0, len(raw) // 2) + raw
        while len(blob) % 4:
            blob += b"\0"
        entries.append((pid, offset + len(body)))
        body += blob
    section = struct.pack("<II", 8 + 8 * len(props) + len(body), len(props))
    for pid, off in entries:
        section += struct.pack("<II", pid, off)
    section += bytes(body)
    head = struct.pack("<HHI16sI", 0xFFFE, 0, 0, b"\0" * 16, 1)
    head += b"\xE0\x85\x9F\xF2\xF9\x4F\x68\x10\xAB\x91\x08\x00\x2B\x27\xB3\xD9" + struct.pack("<I", 48)
    return bytes(head) + section


# --------------------------------------------------------------------------
# Distribution ("배포용") documents: AES-128 encryption for the test writer
# --------------------------------------------------------------------------

def _aes_sbox():
    sbox = [0] * 256
    for x in range(256):
        # multiplicative inverse in GF(2^8) by brute force, then affine transform
        inv = 0
        if x:
            for y in range(1, 256):
                if _gmul(x, y) == 1:
                    inv = y
                    break
        s = inv
        r = s
        for _ in range(4):
            s = ((s << 1) | (s >> 7)) & 0xFF
            r ^= s
        sbox[x] = r ^ 0x63
    return sbox


def _gmul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = ((a << 1) ^ 0x11B) if a & 0x80 else (a << 1)
        b >>= 1
    return r


_SBOX = None


def aes128_encrypt_ecb(key, data):
    """Straightforward (slow) FIPS-197 AES-128 encryption, for tests only."""
    global _SBOX
    if _SBOX is None:
        _SBOX = _aes_sbox()
    sbox = _SBOX
    w = [list(key[4 * i:4 * i + 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [sbox[b] for b in t]
            t[0] ^= rcon
            rcon = _gmul(rcon, 2)
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    out = bytearray()
    for off in range(0, len(data), 16):
        s = list(data[off:off + 16])
        for c in range(4):
            for r in range(4):
                s[4 * c + r] ^= w[c][r]
        for rnd in range(1, 11):
            s = [sbox[b] for b in s]
            s = [s[(4 * ((c + r) % 4)) + r] for c in range(4) for r in range(4)]
            if rnd != 10:
                ns = []
                for c in range(4):
                    a = s[4 * c:4 * c + 4]
                    ns += [_gmul(a[0], 2) ^ _gmul(a[1], 3) ^ a[2] ^ a[3],
                           a[0] ^ _gmul(a[1], 2) ^ _gmul(a[2], 3) ^ a[3],
                           a[0] ^ a[1] ^ _gmul(a[2], 2) ^ _gmul(a[3], 3),
                           _gmul(a[0], 3) ^ a[1] ^ a[2] ^ _gmul(a[3], 2)]
                s = ns
            for c in range(4):
                for r in range(4):
                    s[4 * c + r] ^= w[4 * rnd + c][r]
        out += bytes(s)
    return bytes(out)


def _ms_rand(seed):
    state = seed & 0xFFFFFFFF
    while True:
        state = (state * 214013 + 2531011) & 0xFFFFFFFF
        yield (state >> 16) & 0x7FFF


def distribution_blob(seed, aes_key):
    """Build a 256-byte DISTRIBUTE_DOC_DATA payload hiding ``aes_key``."""
    plain = bytearray((i * 7 + 3) & 0xFF for i in range(256))
    struct.pack_into("<I", plain, 0, seed)
    off = 4 + (seed & 0x0F)
    plain[off:off + 16] = aes_key
    rnd = _ms_rand(seed)
    count = 0
    k = 0
    for i in range(256):
        if count == 0:
            k = next(rnd) & 0xFF
            count = (next(rnd) & 0x0F) + 1
        if i >= 4:
            plain[i] ^= k
        count -= 1
    return bytes(plain)


def build_distribution_hwp(docinfo, section, seed=0x12345678, key=b"0123456789ABCDEF"):
    body = deflate(section)
    body += b"\0" * (-len(body) % 16)
    view = rec(0x1C, 0, distribution_blob(seed, key)) + aes128_encrypt_ecb(key, body)
    streams = {"FileHeader": file_header(True, False, True),
               "DocInfo": deflate(docinfo),
               "ViewText/Section0": view}
    return build_cfb(streams)


# --------------------------------------------------------------------------
# HWPX builder
# --------------------------------------------------------------------------

NS = ('xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
      'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
      'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
      'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core"')


def build_hwpx(section_xmls, header_xml=None, extra=None, title="테스트 문서",
               creator="홍길동", manifest_extra="", encrypted=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip")
        z.writestr("version.xml", '<?xml version="1.0" encoding="UTF-8"?>'
                   '<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" '
                   'tagetApplication="WORDPROCESSOR" major="5" minor="1" micro="1" '
                   'buildNumber="0" os="1" xmlVersion="1.5" application="Hancom Office Hangul" '
                   'appVersion="12, 0, 0, 0"/>')
        z.writestr("META-INF/container.xml", '<?xml version="1.0" encoding="UTF-8"?>'
                   '<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<ocf:rootfiles><ocf:rootfile full-path="Contents/content.hpf" '
                   'media-type="application/hwpml-package+xml"/></ocf:rootfiles></ocf:container>')
        manifest = ('<?xml version="1.0" encoding="UTF-8"?><odf:manifest '
                    'xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">')
        if encrypted:
            manifest += ('<odf:file-entry odf:full-path="Contents/section0.xml">'
                         '<odf:encryption-data odf:checksum-type="SHA1"/></odf:file-entry>')
        manifest += "</odf:manifest>"
        z.writestr("META-INF/manifest.xml", manifest)
        items = ['<opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>']
        spine = []
        for i in range(len(section_xmls)):
            items.append('<opf:item id="section%d" href="Contents/section%d.xml" '
                         'media-type="application/xml"/>' % (i, i))
            spine.append('<opf:itemref idref="section%d" linear="yes"/>' % i)
        hpf = ('<?xml version="1.0" encoding="UTF-8"?><opf:package '
               'xmlns:opf="http://www.idpf.org/2007/opf/" version="" unique-identifier="" id="">'
               '<opf:metadata><opf:title>%s</opf:title><opf:language>ko</opf:language>'
               '<opf:meta name="creator" content="text">%s</opf:meta></opf:metadata>'
               '<opf:manifest>%s%s</opf:manifest><opf:spine>%s</opf:spine></opf:package>'
               % (title, creator, "".join(items), manifest_extra, "".join(spine)))
        z.writestr("Contents/content.hpf", hpf)
        z.writestr("Contents/header.xml", header_xml or (
            '<?xml version="1.0" encoding="UTF-8"?><hh:head %s version="1.5" secCnt="1">'
            '<hh:refList><hh:paraProperties itemCnt="1"><hh:paraPr id="0">'
            '<hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr></hh:paraProperties>'
            '<hh:styles itemCnt="1"><hh:style id="0" type="PARA" name="바탕글" engName="Normal" '
            'paraPrIDRef="0" charPrIDRef="0" nextStyleIDRef="0" langID="1042" lockForm="0"/>'
            '</hh:styles></hh:refList></hh:head>' % NS))
        for i, sx in enumerate(section_xmls):
            z.writestr("Contents/section%d.xml" % i,
                       '<?xml version="1.0" encoding="UTF-8"?><hs:sec %s>%s</hs:sec>' % (NS, sx))
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def hp_p(content, para_pr="0", style="0"):
    return '<hp:p id="0" paraPrIDRef="%s" styleIDRef="%s" pageBreak="0" columnBreak="0" merged="0">%s</hp:p>' % (
        para_pr, style, content)


def hp_run(inner):
    return '<hp:run charPrIDRef="0">%s</hp:run>' % inner


def hp_t(text):
    return "<hp:t>%s</hp:t>" % text
