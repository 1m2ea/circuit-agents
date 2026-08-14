import os, struct

desktop = r"C:\Users\lgw12\Desktop"
target = r"C:\Users\lgw12\WorkBuddy\666\circuit-agents\dist\circuit-agents.exe"
lnk_path = os.path.join(desktop, "电路拓扑工作台.lnk")

CLSID_SHELLINK = bytes([0x01,0x14,0x02,0x00,0x00,0x00,0x00,0x00,
                        0xC0,0x00,0x00,0x00,0x00,0x00,0x00,0x46])

# --- Strings (ANSI + Unicode variants) ---
local_ansi = target.encode("mbcs") + b"\x00"
suffix_ansi = b"\x00\x00"   # 2 bytes keeps the Unicode field 2-byte aligned
local_uni = target.encode("utf-16-le") + b"\x00\x00"
suffix_uni = b"\x00\x00"

# --- VolumeID (DriveType=3 fixed disk, no label) ---
vol_id = (struct.pack("<I", 16) +   # VolumeIDSize
          struct.pack("<I", 3)  +   # DriveType = FIXED
          struct.pack("<I", 0)  +   # VolumeSerialNumber
          struct.pack("<I", 16))    # VolumeLabelOffset (=size => no label)

# --- LinkInfo (Unicode aware: HeaderSize = 36) ---
HDR = 36
li = bytearray()
li += struct.pack("<I", 0)                       # LinkInfoSize (backfilled)
li += struct.pack("<I", HDR)                     # LinkInfoHeaderSize = 36
li += struct.pack("<I", 0x1)                     # LinkInfoFlags = VolumeIDAndLocalBasePath
li += struct.pack("<I", HDR)                     # VolumeIDOffset
off_local = HDR + len(vol_id)
li += struct.pack("<I", off_local)               # LocalBasePathOffset (ANSI)
li += struct.pack("<I", 0)                       # CommonNetworkRelativeLinkOffset (none)
off_csuffix = off_local + len(local_ansi)
li += struct.pack("<I", off_csuffix)             # CommonPathSuffixOffset (ANSI)
off_local_uni = off_csuffix + len(suffix_ansi)
li += struct.pack("<I", off_local_uni)           # LocalBasePathUnicodeOffset
off_csuffix_uni = off_local_uni + len(local_uni)
li += struct.pack("<I", off_csuffix_uni)         # CommonPathSuffixUnicodeOffset
# payload
li += vol_id
li += local_ansi
li += suffix_ansi
li += local_uni
li += suffix_uni
struct.pack_into("<I", li, 0, len(li))           # backfill LinkInfoSize

# --- SHELLLINK_HEADER ---
hdr = bytearray()
hdr += struct.pack("<I", 76)           # HeaderSize
hdr += CLSID_SHELLINK                  # LinkCLSID
hdr += struct.pack("<I", 0x82)        # LinkFlags = HasLinkInfo | IsUnicode
hdr += struct.pack("<I", 0x20)        # FileAttributes = ARCHIVE
hdr += struct.pack("<Q", 0)           # CreationTime
hdr += struct.pack("<Q", 0)           # AccessTime
hdr += struct.pack("<Q", 0)           # WriteTime
hdr += struct.pack("<I", 0)           # FileSize
hdr += struct.pack("<I", 0)           # IconIndex
hdr += struct.pack("<I", 1)           # ShowCommand = SW_SHOWNORMAL
hdr += struct.pack("<H", 0)           # HotKey
hdr += struct.pack("<H", 0)           # Reserved1
hdr += struct.pack("<I", 0)           # Reserved2
hdr += struct.pack("<I", 0)           # Reserved3

data = bytes(hdr) + bytes(li)
with open(lnk_path, "wb") as f:
    f.write(data)
print("written bytes:", len(data))
print("path:", lnk_path)
print("exists:", os.path.exists(lnk_path))
print("target exists:", os.path.exists(target))
