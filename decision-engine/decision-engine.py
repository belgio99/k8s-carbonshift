from flask import Flask, jsonify
import datetime

app = Flask(__name__)

@app.route('/schedule')
def get_decision():
    # Calculate the valid until time as the current time plus 1 hour
    valid_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    valid_until_iso = valid_until.strftime('%Y-%m-%dT%H:%M:%SZ')

    decision = {
      "directWeight": 80,
      "queueWeight": 20,
      "flavorWeights": {
        "low-power": 50,
        "mid-power": 30,
        "high-power": 0
      },
      "deadlines": {
        "low-power": 600,
        "mid-power": 120,
        "high-power": 30
      },
      "validUntil": valid_until_iso
    }
    return jsonify(decision)

@app.route('/healthz', methods=['GET'])
def readiness_check():
    return jsonify({"status": "ready"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
