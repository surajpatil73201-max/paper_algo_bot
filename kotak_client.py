import os

class KotakClient:
    def __init__(self):
        self.consumer_key = os.getenv("KOTAK_CONSUMER_KEY")
        self.consumer_secret = os.getenv("KOTAK_CONSUMER_SECRET")
        self.access_token = os.getenv("KOTAK_ACCESS_TOKEN")
        self.ucc = os.getenv("KOTAK_UCC")
        self.mobile = os.getenv("KOTAK_MOBILE")
        self.mpin = os.getenv("KOTAK_MPIN")
        self.totp = os.getenv("KOTAK_TOTP")

    def login(self):
        return {
            "status": "LOGIN_PLACEHOLDER",
            "message": "Credentials loaded. Real login not enabled yet."
        }

    def place_order(self, payload: dict):
        return {
            "status": "LIVE_PLACEHOLDER_NOT_SENT",
            "message": "Order not sent to Kotak yet",
            "payload": payload
        }
