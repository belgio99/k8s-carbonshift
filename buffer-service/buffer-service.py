import os
import time
import json
import logging
import pika
import requests
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# RabbitMQ Configuration 
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "carbonuser")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "supersecret")
QUEUE_NAME = os.getenv("QUEUE_NAME", "carbonshift-delayed-requests")

# Decision Engine Configuration
DECISION_ENGINE_URL = os.getenv("DECISION_ENGINE_URL", "http://decision-engine:5000")

# Target Service Configuration
TARGET_SERVICE_URL = os.getenv("TARGET_SERVICE_URL", "http://carbonshift-decisional-router")

# RabbitMQ Connection
def get_rabbitmq_connection():
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300
    )
    return pika.BlockingConnection(parameters)

# Producer: adds requests to the queue
def publish_to_queue(request_data, delay_seconds):
    try:
        connection = get_rabbitmq_connection()
        channel = connection.channel()
        
        # Declare the queue
        channel.queue_declare(queue=QUEUE_NAME, durable=True)
        
        # Add execution timestamp
        execute_at = datetime.now() + timedelta(seconds=delay_seconds)
        request_data["execute_at"] = execute_at.isoformat()
        
        # Send the message
        channel.basic_publish(
            exchange='',
            routing_key=QUEUE_NAME,
            body=json.dumps(request_data),
            properties=pika.BasicProperties(
                delivery_mode=2,  # make message persistent
            )
        )
        
        logger.info(f"Request added to queue, to be executed at {execute_at}")
        connection.close()
        return True
    except Exception as e:
        logger.error(f"Error publishing to queue: {e}")
        return False

# Consumer: processes requests from the queue
def process_queue():
    try:
        connection = get_rabbitmq_connection()
        channel = connection.channel()
        
        # Declare the queue
        channel.queue_declare(queue=QUEUE_NAME, durable=True)
        
        def callback(ch, method, properties, body):
            try:
                # Decode the message
                message = json.loads(body)
                execute_at = datetime.fromisoformat(message["execute_at"])
                now = datetime.now()
                
                # Check if it's time to execute the request
                if execute_at <= now:
                    logger.info(f"Processing request from queue")
                    
                    # Remove added metadata
                    original_request = {k: v for k, v in message.items() if k != "execute_at"}
                    
                    # Send the request to the target service
                    response = requests.post(
                        f"{TARGET_SERVICE_URL}/avg",
                        json=original_request["body"],
                        headers=original_request.get("headers", {})
                    )
                    
                    logger.info(f"Request processed with status code: {response.status_code}")
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                else:
                    # Not yet time to execute, put back in queue
                    logger.info(f"Request not yet ready, put back in queue. Wait time: {(execute_at - now).total_seconds()} seconds")
                    ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                    time.sleep(1)  # Small sleep to avoid cycles that are too rapid
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                # In case of error, acknowledge the message anyway to avoid infinite loops
                ch.basic_ack(delivery_tag=method.delivery_tag)
        
        # Configure the consumer to receive one message at a time
        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(queue=QUEUE_NAME, on_message_callback=callback)
        
        logger.info("Consumer started, waiting for messages...")
        channel.start_consuming()
    except Exception as e:
        logger.error(f"Error in consumer: {e}")
        time.sleep(5)  # Retry after 5 seconds
        process_queue()  # Retry

# Start the consumer in a separate thread
consumer_thread = threading.Thread(target=process_queue, daemon=True)
consumer_thread.start()

@app.route('/buffer', methods=['POST'])
def buffer_request():
    """
    Endpoint to insert a request in the buffer
    The original request is saved and scheduled to be executed at a later time
    """
    try:
        # Get urgency level and maximum wait time
        urgency_level = int(request.headers.get('urgency-level', 0))
        max_delay_minutes = int(request.headers.get('max-delay-minutes', 30))
        
        # Query the decision engine for optimal delay
        decision_response = requests.post(
            f"{DECISION_ENGINE_URL}/get-strategy",
            json={
                "urgency_level": urgency_level,
                "max_delay_minutes": max_delay_minutes
            }
        )
        
        if decision_response.status_code != 200:
            return jsonify({"error": "Error communicating with the decision engine"}), 500
        
        decision_data = decision_response.json()
        
        # If we don't need to buffer, forward directly
        if not decision_data.get("should_buffer", False):
            return jsonify({
                "message": "The request does not need buffering", 
                "strategy": decision_data.get("strategy")
            }), 200
        
        # Prepare data for the queue
        minutes_to_wait = decision_data.get("minutes_to_wait", 0)
        seconds_to_wait = minutes_to_wait * 60
        
        # Save the original request
        request_data = {
            "body": request.get_json() if request.is_json else {},
            "headers": dict(request.headers),
            "method": request.method
        }
        
        # Publish to queue
        success = publish_to_queue(request_data, seconds_to_wait)
        
        if success:
            return jsonify({
                "message": "Request inserted in buffer",
                "minutes_to_wait": minutes_to_wait,
                "execute_at": (datetime.now() + timedelta(minutes=minutes_to_wait)).isoformat()
            }), 202
        else:
            return jsonify({"error": "Error inserting into buffer"}), 500
    
    except Exception as e:
        logger.error(f"Error handling buffer request: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", "5001")))