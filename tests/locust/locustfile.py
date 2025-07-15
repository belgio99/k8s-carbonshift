from locust import HttpUser, task, between
import logging

class APILoadTester(HttpUser):
    wait_time = between(1, 3)
    host = "http://localhost:8000"
    
    @task
    def post_numbers(self):
        payload = {
            "numbers": [1, 2, 3, 50, 500, 1000]
        }
        with self.client.post("/avg", json=payload, timeout=10, catch_response=True) as response:
            if response.status_code == 200:
                logging.info("Request OK: %s", response.status_code)
                response.success()
            else:
                logging.warning("Request not OK: %s", response.status_code)
                response.failure("Status code not 200")
