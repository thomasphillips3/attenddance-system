"""Tap-In: NFC fob and card check-in.

credentials.py mints and resolves tags; service.py turns one tap into an
attendance change. Neither touches the Flask request, so the iPhone tag URL,
the Android kiosk, the USB reader and offline replay all share one path.
"""
