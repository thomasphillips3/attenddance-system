"""
Configuration settings for AttenDANCE system
"""

import os
from datetime import timedelta

# Project root directory
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

class Config:
    """Base configuration class"""

    # Basic Flask settings
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production-12345'

    # Database settings
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        f'sqlite:///{os.path.join(BASE_DIR, "data", "attendance.db")}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }
    
    # Session settings
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    SESSION_COOKIE_SECURE = False  # Set to True in production with HTTPS
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

    # "Remember me" persistent-login cookie is a long-lived credential — give it
    # the same hardening as the session cookie. SECURE is flipped on in prod.
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SECURE = False  # -> True in ProductionConfig
    REMEMBER_COOKIE_DURATION = timedelta(days=14)
    
    # JWT settings
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY') or SECRET_KEY
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=24)
    
    # RFID settings
    RFID_ENABLED = os.environ.get('RFID_ENABLED', 'true').lower() == 'true'
    RFID_SPI_DEV = int(os.environ.get('RFID_SPI_DEV', 0))
    RFID_SPI_SPEED = int(os.environ.get('RFID_SPI_SPEED', 1000000))
    RFID_RST_PIN = int(os.environ.get('RFID_RST_PIN', 25))
    
    # Application settings
    APP_NAME = 'LSO Dance'
    APP_VERSION = '1.0.0'
    # The studio's public website. The logo on the sign-in and sign-up pages
    # links here, so a parent who lands on either can get back to the main site.
    STUDIO_URL = os.environ.get('STUDIO_URL', 'https://www.lsodance.com')
    TIMEZONE = os.environ.get('TIMEZONE', 'America/New_York')
    
    # File upload settings
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'uploads')
    
    # Pagination settings
    STUDENTS_PER_PAGE = 50
    ATTENDANCE_PER_PAGE = 100
    
    # Square payments
    SQUARE_ACCESS_TOKEN = os.environ.get('SQUARE_ACCESS_TOKEN')
    SQUARE_LOCATION_ID = os.environ.get('SQUARE_LOCATION_ID')
    SQUARE_ENVIRONMENT = os.environ.get('SQUARE_ENVIRONMENT', 'sandbox')  # sandbox or production

    # Email settings (for future notifications)
    MAIL_SERVER = os.environ.get('MAIL_SERVER')
    MAIL_PORT = int(os.environ.get('MAIL_PORT') or 587)
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_REPLY_TO = os.environ.get('MAIL_REPLY_TO', 'LaShellesDance@gmail.com')
    
class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    TESTING = False
    
class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True  # persistent-login cookie only over HTTPS

class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    RFID_ENABLED = False

# Configuration dictionary
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
} 