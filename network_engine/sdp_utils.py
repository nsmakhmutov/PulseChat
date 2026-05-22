def normalize_sdp_ice(sdp: str) -> str:

    sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(sep)

    first_ufrag = None
    first_pwd   = None
    for line in lines:
        if line.startswith("a=ice-ufrag:") and first_ufrag is None:
            first_ufrag = line.split(":", 1)[1].strip()
        if line.startswith("a=ice-pwd:") and first_pwd is None:
            first_pwd = line.split(":", 1)[1].strip()
        if first_ufrag and first_pwd:
            break

    if not first_ufrag or not first_pwd:
        return sdp

    result = []
    for line in lines:
        if line.startswith("a=ice-ufrag:"):
            result.append(f"a=ice-ufrag:{first_ufrag}")
        elif line.startswith("a=ice-pwd:"):
            result.append(f"a=ice-pwd:{first_pwd}")
        else:
            result.append(line)

    return sep.join(result)


def patch_audio_bitrate(sdp: str, bitrate_kbps: int) -> str:
    sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(sep)
    result = []
    in_audio = False
    for line in lines:
        if line.startswith("m=audio"):
            in_audio = True
        elif line.startswith("m="):
            in_audio = False

        if in_audio and line.startswith("c=") and not any(l.startswith("b=AS:") for l in result[-3:]):
            result.append(line)
            result.append(f"b=AS:{bitrate_kbps}")
            continue
        result.append(line)
    return sep.join(result)


def patch_opus_fec(sdp: str) -> str:
    sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(sep)
    opus_pt = None

    for line in lines:
        if "opus/48000" in line and line.startswith("a=rtpmap:"):
            opus_pt = line.split(":")[1].split()[0]
            break

    if not opus_pt:
        return sdp

    result = []
    fmtp_found = False
    for line in lines:
        if line.startswith(f"a=fmtp:{opus_pt}"):
            fmtp_found = True
            params = line.split(" ", 1)[1] if " " in line else ""
            if "useinbandfec" not in params:
                params += ";useinbandfec=1"
            if "usedtx" not in params:
                params += ";usedtx=1"
            result.append(f"a=fmtp:{opus_pt} {params}")
        else:
            result.append(line)

    if not fmtp_found:
        final = []
        for line in result:
            final.append(line)
            if f"a=rtpmap:{opus_pt}" in line:
                final.append(f"a=fmtp:{opus_pt} minptime=10;useinbandfec=1;usedtx=1")
        result = final

    return sep.join(result)
