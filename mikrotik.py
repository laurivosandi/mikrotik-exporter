#!/usr/bin/env python
import os
from aio_api_ros import create_rosapi_connection
from aiostream import stream
from sanic import Sanic, response, exceptions

app = Sanic("exporter")

PREFIX = os.getenv("PROMETHEUS_PREFIX", "mikrotik_")
PROMETHEUS_BEARER_TOKEN = os.getenv("PROMETHEUS_BEARER_TOKEN")
MIKROTIK_USER = os.getenv("MIKROTIK_USER")
MIKROTIK_PASSWORD = os.getenv("MIKROTIK_PASSWORD")
TARGETS = os.getenv("TARGETS")

if not MIKROTIK_USER:
    raise ValueError("MIKROTIK_USER not specified")
if not MIKROTIK_PASSWORD:
    raise ValueError("MIKROTIK_PASSWORD not specified")
if not TARGETS:
    raise ValueError("TARGETS not specified")

RATE_MAPPING = {
    "40Gbps": 40 * 10 ** 9,
    "10Gbps": 10 * 10 ** 9,
    "1Gbps": 10 ** 9,
    "100Mbps": 100 * 10 ** 6,
    "10Mbps": 10 * 10 ** 6,
}


async def wrap(i):
    metrics_seen = set()
    async for name, tp, value, labels in i:
        if name not in metrics_seen:
            yield "# TYPE %s %s" % (PREFIX + name, tp)
            metrics_seen.add(name)
        yield "%s%s %s" % (
            PREFIX + name,
            ("{%s}" % ",".join(["%s=\"%s\"" % j for j in labels.items()]) if labels else ""),
            value)


async def scrape_mikrotik(target):
    mk = await create_rosapi_connection(
        mk_ip=target,
        mk_port=8728,
        mk_user=MIKROTIK_USER,
        mk_psw=MIKROTIK_PASSWORD,

    )

    ports = ",".join([str(j) for j in range(0, 24)])

    mk.talk_sentence(["/interface/print"])
    res = await mk.read_full_answer()
    for resp, _, obj in res:
        if resp in ("!trap", "!done"):
            break
        labels = {"host": target, "port": obj["name"], "type": obj["type"]}

        yield "interface-rx-bytes", "counter", obj["rx-byte"], labels
        yield "interface-tx-bytes", "counter", obj["tx-byte"], labels
        yield "interface-rx-packets", "counter", obj["rx-packet"], labels
        yield "interface-tx-packets", "counter", obj["tx-packet"], labels
        try:
            yield "interface-rx-errors", "counter", obj["rx-error"], labels
            yield "interface-tx-errors", "counter", obj["tx-error"], labels
        except KeyError:
            pass
        try:
            yield "interface-rx-drops", "counter", obj["rx-drop"], labels
            yield "interface-tx-drops", "counter", obj["tx-drop"], labels
        except KeyError:
            pass
        yield "interface-running", "gauge", int(obj["tx-byte"]), labels
        yield "interface-actual-mtu", "gauge", obj["actual-mtu"], labels

    mk.talk_sentence(["/interface/ethernet/monitor", "=once=", "=numbers=%s" % ports])
    res = await mk.read_full_answer()
    for resp, _, obj in res:
        if resp in ("!trap", "!done"):
            break
        labels = {"host": target, "port": obj["name"]}

        try:
            rate = obj["rate"]
        except KeyError:
            pass
        else:
            yield "interface-rate", "gauge", RATE_MAPPING[rate], labels

        try:
            labels["sfp-vendor-name"] = obj["sfp-vendor-name"]
        except KeyError:
            pass
        try:
            labels["sfp-vendor-part-number"] = obj["sfp-vendor-part-number"]
        except KeyError:
            pass

        try:
            yield "interface-sfp-temperature", "gauge", obj["sfp-temperature"], labels
            yield "interface-sfp-tx-power", "gauge", obj["sfp-tx-power"], labels
            yield "interface-sfp-rx-power", "gauge", obj["sfp-tx-power"], labels
        except KeyError:
            pass

        labels["status"] = obj["status"]
        try:
            labels["sfp-module-present"] = int(obj["sfp-module-present"])
        except KeyError:
            pass
        yield "interface-status", "gauge", 1, labels

    mk.talk_sentence(["/interface/ethernet/poe/monitor", "=once=", "=numbers=%s" % ports])
    res = await mk.read_full_answer()
    for resp, _, obj in res:
        if resp in ("!trap", "!done"):
            break

        labels = {"host": target, "port": obj["name"]}
        try:
            yield "poe-out-voltage", "gauge", float(obj["poe-out-voltage"]), labels
            yield "poe-out-current", "gauge", int(obj["poe-out-current"]) / 1000.0, labels
        except KeyError:
            pass

        labels["status"] = obj["poe-out-status"]
        yield "poe-out-status", "gauge", 1, labels

    mk.talk_sentence(["/system/resource/print"])
    res = await mk.read_full_answer()
    for resp, _, obj in res:
        if resp in ("!trap", "!done"):
            break

        labels = {"host": target}
        yield "system-write-sect-total", "counter", obj["write-sect-total"], labels
        yield "system-free-memory", "gauge", obj["free-memory"], labels
        try:
            yield "system-bad-blocks", "counter", obj["bad-blocks"], labels
        except KeyError:
            pass

        for key in ("version", "cpu", "cpu-count", "board-name", "architecture-name"):
            labels[key] = obj[key]
        yield "system-version", "gauge", 1, labels

    mk.talk_sentence(["/system/health/print"])
    res = await mk.read_full_answer()
    for resp, _, obj in res:
        if resp in ("!trap", "!done"):
            break
        for key, value in obj.items():
            labels = {"host": target}
            try:
                value = float(value)
            except ValueError:
                labels["state"] = value
                yield "system-health-%s" % key, "gauge", 1, labels
            else:
                yield "system-health-%s" % key, "gauge", value, labels
    mk.close()


@app.route("/metrics")
async def view_export(request):
    if PROMETHEUS_BEARER_TOKEN and request.token != PROMETHEUS_BEARER_TOKEN:
        raise exceptions.Forbidden("Invalid bearer token")

    async def streaming_fn(response):
        args = [scrape_mikrotik(target) for target in TARGETS.split(",")]
        combine = stream.merge(*args)
        async with combine.stream() as streamer:
            async for line in wrap(streamer):
                await response.write(line + "\n")

    return response.stream(streaming_fn, content_type="text/plain")


app.run(host="0.0.0.0", port=3001)
