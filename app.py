import os
import math
import logging
from datetime import datetime, date, timedelta
from flask import Flask
from extensions import db, socketio, login_manager
from models import User, Schedule, Stop, Booking, DriverLocation, Notification

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Telegram bot instance (global)
telegram_app = None


def haversine(lat1, lon1, lat2, lon2):
    """Distance in meters between two lat/lon points."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def send_telegram(chat_id, text):
    """Send a Telegram message if bot is configured."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if not token or not chat_id:
        return
    import requests
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text},
            timeout=5
        )
    except Exception as e:
        logger.warning(f'Telegram send failed: {e}')


def create_app():
    app = Flask(__name__)

    # Config
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
    _basedir = os.path.abspath(os.path.dirname(__file__))
    db_url = os.environ.get('DATABASE_URL', f'sqlite:///{_basedir}/data/noveebus.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Init extensions
    db.init_app(app)
    socketio.init_app(app, async_mode='threading', cors_allowed_origins='*')
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'warning'

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Register blueprints
    from routes.auth import auth_bp
    from routes.employee import employee_bp
    from routes.driver import driver_bp
    from routes.admin import admin_bp
    from routes.telegram import telegram_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(employee_bp, url_prefix='/employee')
    app.register_blueprint(driver_bp, url_prefix='/driver')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(telegram_bp)

    # Root redirect
    from flask import redirect, url_for
    from flask_login import current_user

    @app.route('/')
    def index():
        if current_user.is_authenticated:
            if current_user.role == 'admin':
                return redirect(url_for('admin.dashboard'))
            elif current_user.role == 'driver':
                return redirect(url_for('driver.dashboard'))
            else:
                return redirect(url_for('employee.dashboard'))
        return redirect(url_for('auth.login'))

    # SocketIO events
    from flask_socketio import emit, join_room
    from flask_login import current_user as cu

    @socketio.on('connect')
    def on_connect():
        logger.info(f'Client connected')

    @socketio.on('join_schedule')
    def on_join_schedule(data):
        schedule_id = data.get('schedule_id')
        if schedule_id:
            join_room(f'schedule_{schedule_id}')

    @socketio.on('join_admin')
    def on_join_admin():
        join_room('admin_room')

    @socketio.on('driver_location')
    def on_driver_location(data):
        schedule_id = data.get('schedule_id')
        lat = data.get('lat')
        lon = data.get('lon')

        if not all([schedule_id, lat, lon]):
            return

        with app.app_context():
            # Save location
            schedule = Schedule.query.get(schedule_id)
            if not schedule:
                return

            loc = DriverLocation(
                driver_id=schedule.driver_id,
                schedule_id=schedule_id,
                lat=lat,
                lon=lon
            )
            db.session.add(loc)
            db.session.commit()

            # Broadcast to passengers watching this schedule
            emit('location_update', {
                'schedule_id': schedule_id,
                'lat': lat,
                'lon': lon,
                'timestamp': datetime.utcnow().isoformat()
            }, room=f'schedule_{schedule_id}')

            # Broadcast to admin room
            emit('location_update', {
                'schedule_id': schedule_id,
                'lat': lat,
                'lon': lon,
                'timestamp': datetime.utcnow().isoformat()
            }, room='admin_room')

            # Check proximity to stops → notify booked passengers
            stops = schedule.route.stops
            for stop in stops:
                dist = haversine(lat, lon, stop.lat, stop.lon)
                if dist <= 500:
                    # Notify booked passengers at this stop
                    bookings = Booking.query.filter(
                        Booking.schedule_id == schedule_id,
                        Booking.status.in_(['booked', 'waitlisted'])
                    ).all()
                    for booking in bookings:
                        user = booking.user
                        msg = f'🚌 NoveeBus: Your bus is arriving at {stop.name} soon! Please be ready.'
                        if user.telegram_chat_id:
                            send_telegram(user.telegram_chat_id, msg)
                        # Save notification
                        notif = Notification(user_id=user.id, message=msg, channel='telegram')
                        db.session.add(notif)
            db.session.commit()

    # APScheduler for day-before reminders
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()

        def send_reminders():
            with app.app_context():
                tomorrow = date.today() + timedelta(days=1)
                schedules = Schedule.query.filter_by(date=tomorrow, status='scheduled').all()
                for sched in schedules:
                    bookings = Booking.query.filter(
                        Booking.schedule_id == sched.id,
                        Booking.status == 'booked'
                    ).all()
                    for booking in bookings:
                        user = booking.user
                        msg = (f'⏰ NoveeBus Reminder: You have a bus tomorrow!\n'
                               f'Route: {sched.route.name}\n'
                               f'Shift: {sched.shift.name} at {sched.shift.departure_time}\n'
                               f'Date: {sched.date.strftime("%d %b %Y")}')
                        if user.telegram_chat_id:
                            send_telegram(user.telegram_chat_id, msg)
                        notif = Notification(user_id=user.id, message=msg, channel='telegram')
                        db.session.add(notif)
                db.session.commit()

        scheduler.add_job(send_reminders, 'cron', hour=19, minute=0)
        scheduler.start()
    except Exception as e:
        logger.warning(f'Scheduler init failed: {e}')

    return app


app = create_app()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=8089, debug=True)
