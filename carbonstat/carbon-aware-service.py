from datetime import datetime
from os import environ
from enum import Enum
from flask import Flask, request, jsonify

# ------ STRATEGIES ------
# Import and enum carbon-aware strategies (aka. flavours)
from flavours.interface import CarbonAwareStrategy
from flavours.low_power import LowPowerStrategy
from flavours.medium_power import MediumPowerStrategy
from flavours.high_power import HighPowerStrategy


class CarbonAwareStrategies(Enum):
    low = LowPowerStrategy
    mid = MediumPowerStrategy
    high = HighPowerStrategy


# ------ CONTEXT ------
# Carbon-aware context
class Context:
    def __init__(self):
        flavour = environ.get("FLAVOUR", "high-power")  # Default to HighPower if not set
        if flavour not in CarbonAwareStrategies.__members__:
            raise ValueError(
                f"Flavour '{flavour}' is not valid. Use: {list(CarbonAwareStrategies.__members__.keys())}"
            )
        print("Starting service with flavour:", flavour, "power")
        self.strategy = CarbonAwareStrategies[flavour].value

    def getCarbonAwareStrategy(self) -> CarbonAwareStrategy:
        return self.strategy


# ------ SERVICE ------
app = Flask(__name__)
# set service's context
app.context = Context()


@app.route("/avg", methods=["POST"])
def avg():
    request_data = request.get_json()
    if "numbers" not in request_data or not isinstance(request_data["numbers"], list):
        return jsonify({"error": "Missing 'numbers' list in request"}), 400

    numbers = request_data["numbers"]
    strategy = app.context.getCarbonAwareStrategy()

    start = datetime.now()
    total = strategy.avg(numbers)
    end = datetime.now()
    elapsed = round((end.timestamp() - start.timestamp()) * 1000, 2)  # in msec

    return jsonify({"avg": total, "elapsed": elapsed, "strategy": strategy.nop()})


@app.route('/healthz', methods=['GET'])
def readiness_check():
    return jsonify({"status": "ready"}), 200


app.run(host="0.0.0.0", port=80)
