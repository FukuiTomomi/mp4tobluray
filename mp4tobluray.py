# -*- coding: utf-8 -*-
"""
mp4 -> Blu-ray(BDMV) 変換スクリプト

使い方:
  python mp4tobluray.py input.mp4 output_dir BD_DRIVE mode

mode:
  0 : 先頭5分だけでISO作成まで通す動作確認 (PC上の検査用)
  1 : 全編をISOまで作成
  2 : mode 1 で作ったISOをBD-Rに焼く

例:
  python mp4tobluray.py "D:\\movie\\input.mp4" D:\\output E: 0

途中で止まっても、できているファイルは再利用して続きから実行します。
エンコード設定(keyint, fps, preset等)を変えたときは、
output_dir内の .264 と _2pass-0.log と _2pass-0.log.mbtree を手で削除してください。
"""
import json
import os
import re
import shutil
import subprocess
import sys

# ---- 外部ツール (PATHに無ければフルパスに書き換える) ----
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
TSMUXER = "tsMuxeR"
IMGBURN = "ImgBurn.exe"

# ---- 設定 ----
PRESET = "slow"              # 時間優先なら "medium"
DISC_BYTES = 25_025_314_816  # BD-R 25GB
AUDIO_KBPS = 448
MAX_VIDEO_KBPS = 18000       # 映像ビットレートの上限
TEST_SECONDS = 300           # mode 0 の長さ


def run(cmd):
    print("RUN:", " ".join(cmd), flush=True)
    subprocess.check_call(cmd)


def exists(path, min_bytes=1_000_000):
    return os.path.isfile(path) and os.path.getsize(path) > min_bytes


# ---------------------------------------------------------------
# 入力の調査とプロファイル選択
# ---------------------------------------------------------------
def _fr(s):
    try:
        n, d = s.split("/")
        return float(n) / float(d) if float(d) else 0.0
    except Exception:
        return 0.0


def probe(path):
    out = subprocess.check_output([
        FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate:format=duration",
        "-of", "json", path])
    j = json.loads(out)
    s = j["streams"][0]
    return dict(w=s["width"], h=s["height"],
                avg=_fr(s["avg_frame_rate"]), r=_fr(s["r_frame_rate"]),
                dur=float(j["format"]["duration"]))


def choose_profile(i):
    """元の高さとfpsから、Blu-ray規格内の形式を選ぶ(拡大はしない)。"""
    f = i["r"] if 0 < i["r"] <= 120 else i["avg"]   # 可変fpsは最大側を採用
    h = i["h"]
    if f < 24.5:
        if h <= 720:
            return dict(name="720p23.976", w=1280, h=720, fps="24000/1001",
                        tsfps="23.976", keyint=24, fake_il=False)
        return dict(name="1080p23.976", w=1920, h=1080, fps="24000/1001",
                    tsfps="23.976", keyint=24, fake_il=False)
    if f < 45 or h > 1000:
        return dict(name="1080i29.97", w=1920, h=1080, fps="30000/1001",
                    tsfps="29.97", keyint=30, fake_il=True)
    return dict(name="720p59.94", w=1280, h=720, fps="60000/1001",
                tsfps="59.94", keyint=60, fake_il=False)


def calc_video_kbps(dur, margin=0.94, overhead=1.07):
    audio_bytes = dur * AUDIO_KBPS * 1000 / 8
    video_bytes = (DISC_BYTES * margin - audio_bytes) / overhead
    return int(min(video_bytes * 8 / dur / 1000, MAX_VIDEO_KBPS))


# ---------------------------------------------------------------
# エンコード
# ---------------------------------------------------------------
def x264_opts(p):
    o = ["bluray-compat=1", "force-cfr=1", "open-gop=1",
         f"keyint={p['keyint']}", "min-keyint=1", "bframes=3", "ref=3",
         "slices=4", "aud=1", "nal-hrd=vbr",
         "vbv-maxrate=30000", "vbv-bufsize=30000",
         "colorprim=bt709", "transfer=bt709", "colormatrix=bt709"]
    if p["fake_il"]:
        o += ["fake-interlaced=1", "tff=1"]
    return ":".join(o)


def encode_video(src, h264, p, kbps, limit=None):
    vf = (f"setpts=PTS-STARTPTS,"
          f"scale={p['w']}:{p['h']}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={p['w']}:{p['h']}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
          f"fps={p['fps']}")  # 可変fps -> 固定fps
    head = [FFMPEG, "-y", "-i", src] + (["-t", str(limit)] if limit else [])
    opts = ["-an", "-vf", vf, "-c:v", "libx264", "-preset", PRESET,
            "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-b:v", f"{kbps}k", "-x264opts", x264_opts(p)]
    log = os.path.splitext(h264)[0] + "_2pass"
    if exists(log + "-0.log", 1000) and exists(log + "-0.log.mbtree", 1000):
        print("パス1の解析ファイルあり -> パス1をスキップ")
    else:
        run(head + opts + ["-pass", "1", "-passlogfile", log,
                           "-f", "null", os.devnull])
    # 途中で止まった .264 を完成品と誤認しないよう、一時名で書いてから改名
    tmp = h264 + ".part"
    run(head + opts + ["-pass", "2", "-passlogfile", log, "-f", "h264", tmp])
    os.replace(tmp, h264)


def encode_audio(src, ac3, limit=None):
    head = [FFMPEG, "-y", "-i", src] + (["-t", str(limit)] if limit else [])
    tmp = ac3 + ".part"
    run(head + ["-vn", "-af", "aresample=async=1:first_pts=0",
                "-c:a", "ac3", "-b:a", f"{AUDIO_KBPS}k",
                "-ar", "48000", "-ac", "2", "-f", "ac3", tmp])
    os.replace(tmp, ac3)


# ---------------------------------------------------------------
# 多重化 (tsMuxer)
# ---------------------------------------------------------------
def es_to_bdmv(h264, ac3, bdmv_dir, p):
    meta = os.path.splitext(h264)[0] + "_tsMuxer_meta.txt"
    with open(meta, "w", encoding="utf-8", newline="\n") as f:
        f.write("MUXOPT --blu-ray --vbr --auto-chapters=5 --vbv-len=500\n")
        f.write(f'V_MPEG4/ISO/AVC, "{os.path.abspath(h264)}", '
                f'fps={p["tsfps"]}, insertSEI, contSPS\n')
        f.write(f'A_AC3, "{os.path.abspath(ac3)}"\n')
    run([TSMUXER, os.path.abspath(meta), os.path.abspath(bdmv_dir)])


# ---------------------------------------------------------------
# 検査 (PCでできる機械的なチェック。Regzaでの再生保証ではない)
# ---------------------------------------------------------------
def dir_size(d):
    return sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(d) for f in fs)


def validate(bdmv_dir, p, check_sec=600):
    ok = True
    size = dir_size(bdmv_dir)
    limit = DISC_BYTES * 0.97
    print(f"[検査] 合計サイズ {size / 2**30:.2f} GiB / 上限 {limit / 2**30:.2f} GiB")
    if size > limit:
        print("  NG: 容量オーバー")
        ok = False
    for must in ("BDMV", "CERTIFICATE"):
        if not os.path.isdir(os.path.join(bdmv_dir, must)):
            print(f"  NG: {must} フォルダがない")
            ok = False
    m2ts = os.path.join(bdmv_dir, "BDMV", "STREAM", "00000.m2ts")
    if not os.path.isfile(m2ts):
        print("  NG: 00000.m2ts がない")
        return False
    info = json.loads(subprocess.check_output([
        FFPROBE, "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate",
        "-of", "json", m2ts]))
    for s in info["streams"]:
        print("  ", s)
        if s["codec_type"] == "audio" and s.get("sample_rate") != "48000":
            print("  NG: 音声が48kHzでない")
            ok = False
        if s["codec_type"] == "video" and (s["width"], s["height"]) != (p["w"], p["h"]):
            print("  NG: 解像度が想定と違う")
            ok = False
    out = subprocess.check_output([
        FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-read_intervals", f"%+{check_sec}",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", m2ts]).decode()
    keys = []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) > 1 and parts[1].startswith("K") and parts[0] not in ("", "N/A"):
            keys.append(float(parts[0]))
    keys.sort()
    gaps = [b - a for a, b in zip(keys, keys[1:])]
    if gaps:
        print(f"  キーフレーム間隔 最大 {max(gaps):.2f} 秒")
        if max(gaps) > 1.1:
            print("  NG: GOPが1秒を超えている")
            ok = False
    else:
        print("  注意: キーフレーム間隔を取得できなかった")
    print("[検査結果]", "OK" if ok else "NGあり")
    return ok


# ---------------------------------------------------------------
# ISO作成と書き込み (ImgBurn)
# ---------------------------------------------------------------
def bdmv_to_iso(bdmv_dir, output_iso, label="MYBD"):
    # UDF 2.50。ImgBurnのコマンドライン引数は環境で要確認
    try:
        run([IMGBURN, "/MODE", "BUILD", "/BUILDMODE", "IMAGEFILE",
             "/SRC", os.path.abspath(bdmv_dir), "/DEST", os.path.abspath(output_iso),
             "/FILESYSTEM", "UDF", "/UDFREVISION", "2.50",
             "/VOLUMELABEL", label, "/ROOTFOLDER", "YES",
             "/OVERWRITE", "YES", "/START", "/CLOSESUCCESS"])
    except Exception as e:
        print("ImgBurnでのISO作成に失敗:", e)
        print("-> ImgBurnのGUIでBuildモードを選び、UDF 2.50でISOを作成してください。")
        print("   入力フォルダ:", os.path.abspath(bdmv_dir))
        print("   (フォルダ直下に BDMV と CERTIFICATE があること)")
        return
    print("ISO作成完了:", output_iso)
    print("-> ISOをマウントし、直下に BDMV と CERTIFICATE があるか確認してください。")


def burn_iso_to_bluray(iso_path, device):
    run([IMGBURN, "/MODE", "WRITE", "/SRC", iso_path, "/DEST", device,
         "/SPEED", "4", "/VERIFY", "YES", "/START"])
    print("書き込み完了")


# ---------------------------------------------------------------
# メイン
# ---------------------------------------------------------------
def safe_name(path):
    """日本語・空白・記号を含むファイル名を、中間ファイル用のASCII名にする。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return s or "video"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 5:
        print("使い方: python mp4tobluray.py input.mp4 output_dir BD_DRIVE mode")
        print("mode 0:5分テスト / 1:全編ISO作成 / 2:ISOを焼く")
        sys.exit(1)

    src, out_dir, device, mode = sys.argv[1:5]
    src = os.path.abspath(src)
    out_dir = os.path.abspath(out_dir)
    if mode not in ("0", "1", "2"):
        print("modeは 0 / 1 / 2 のいずれかです")
        sys.exit(1)
        
    if mode == "0":
        out_dir = os.path.join(out_dir, "test")
    os.makedirs(out_dir, exist_ok=True)

    base = safe_name(src)
    limit = TEST_SECONDS if mode == "0" else None
    if mode == "0":
        base += "_test"
    h264 = os.path.join(out_dir, base + ".264")
    ac3 = os.path.join(out_dir, base + ".ac3")
    bdmv = os.path.join(out_dir, base + "_BDMV")
    iso = os.path.join(out_dir, base + ".iso")

    if mode == "2":
        if not exists(iso, 100_000_000):
            print("ISOが見つかりません:", iso)
            sys.exit(1)
        burn_iso_to_bluray(iso, device)
        return

    info = probe(src)
    prof = choose_profile(info)
    dur = min(info["dur"], limit) if limit else info["dur"]
    kbps = calc_video_kbps(dur)
    print(f"元: {info['w']}x{info['h']} avg={info['avg']:.2f} r={info['r']:.2f} "
          f"-> {prof['name']} / 映像 {kbps}kbps / preset {PRESET}")

    if exists(h264):
        print("映像(.264)あり -> エンコードをスキップ")
    else:
        encode_video(src, h264, prof, kbps, limit)
    if exists(ac3, 100_000):
        print("音声(.ac3)あり -> スキップ")
    else:
        encode_audio(src, ac3, limit)

    # 多重化以降は設定を変えて何度でもやり直せるよう、毎回作り直す
    if os.path.isdir(bdmv):
        shutil.rmtree(bdmv)
    es_to_bdmv(h264, ac3, bdmv, prof)
    if validate(bdmv, prof):
        bdmv_to_iso(bdmv, iso)
    else:
        print("検査NGのためISO作成を中止しました")


if __name__ == "__main__":
    main()