from flask import Flask, jsonify, request
from prometheus_client import Gauge, start_http_server, CONTENT_TYPE_LATEST
import threading, datetime, math, time
import os

app = Flask(__name__)

# ---- Prometheus metrics ----
g_direct  = Gauge('schedule_direct_weight',  'Direct weight in use')
g_queue   = Gauge('schedule_queue_weight',   'Queue weight in use')
g_flavour = Gauge('schedule_flavour_weight', 'Weight per flavour', ['flavour'])
g_valid   = Gauge('schedule_valid_until',    'UNIX epoch of validUntil')

# ---- shared state ----
schedule_lock   = threading.Lock()
current_schedule = None
step_counter     = 0            # increments by 1 each minute


def round10(x):
    """arrotonda al multiplo di 10 più vicino (0,10,20,…)"""
    return int(round(x / 10.0)) * 10

def fix_sum_to_100(vals):
    """
    Riceve lista di int multipli di 10.
    Se la somma non fa 100, aggiusta il valore più grande
    (o il primo in caso di parità) per far tornare 100.
    """
    diff = 100 - sum(vals)
    if diff != 0:
        i = vals.index(max(vals))
        vals[i] += diff
    return vals

# ---------- helper per i pesi ----------
def generate_weights(step: int):
    """Returns a tuple (direct, queue, flavours_dict), all multiples of 10"""
    # sinusoid for directWeight
    raw_direct = 60 + 30 * math.sin(step * math.pi / 6)        # 30..90
    direct = round10(raw_direct)
    queue  = 100 - direct

    # tre sinusoidi sfasate di 120°
    raw_low  = 50 +  30 * math.sin(step * math.pi / 6)
    raw_mid  = 50 +  30 * math.sin(step * math.pi / 6 + 2*math.pi/3)
    raw_high = 50 +  30 * math.sin(step * math.pi / 6 + 4*math.pi/3)

    low  = round10(raw_low)
    mid  = round10(raw_mid)
    high = round10(raw_high)

    low, mid, high = fix_sum_to_100([low, mid, high])

    return direct, queue, {
        "low-power":  low,
        "mid-power":  mid,
        "high-power": high
    }

def make_schedule(step: int):
    direct, queue, flavours = generate_weights(step)
    valid_until = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(minutes=1))
    return {
        "directWeight": direct,
        "queueWeight":  queue,
        "flavourWeights": flavours,
        "deadlines": {"low-power":600, "mid-power":120, "high-power":40},
        "consumptionEnabled": True,
        "validUntil": valid_until.strftime('%Y-%m-%dT%H:%M:%SZ')
    }

def update_metrics(sched):
    g_direct.set(sched['directWeight'])
    g_queue.set(sched['queueWeight'])
    for flav, w in sched['flavourWeights'].items():
        g_flavour.labels(flav).set(w)
    valid_epoch = datetime.datetime.strptime(
        sched['validUntil'], '%Y-%m-%dT%H:%M:%SZ'
    ).replace(tzinfo=datetime.timezone.utc).timestamp()
    g_valid.set(valid_epoch)

def scheduler_loop():
    global current_schedule, step_counter
    while True:
        step_counter += 1
        new_sched = make_schedule(step_counter)

        with schedule_lock:
            current_schedule = new_sched
        update_metrics(new_sched)

        # wait until the start of the next minute
        now = datetime.datetime.now()
        sleep_sec = 60 - now.second - now.microsecond/1e6
        time.sleep(max(1, sleep_sec))

# ---------- API ----------
@app.route('/schedule')
def get_decision():
    with schedule_lock:
        return jsonify(current_schedule)

@app.route('/setschedule', methods=['POST'])
def set_schedule():
    """Allows manual override."""
    global current_schedule, step_counter
    data = request.get_json()
    with schedule_lock:
        current_schedule = data
        step_counter += 1          # restart from the new state
    update_metrics(data)
    return jsonify({"status": "schedule set"}), 200

@app.route('/healthz')
def health():
    return jsonify({"status": "ready"}), 200


if __name__ == '__main__':
    # start Prometheus metrics server
    thr = threading.Thread(target=scheduler_loop, daemon=True)
    thr.start()
    metrics_port = int(os.getenv("METRICS_PORT", "8001"))
    print("Starting Prometheus metrics server on port ", metrics_port)
    start_http_server(metrics_port)

    app.run(host='0.0.0.0', port=80)