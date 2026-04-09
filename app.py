"""
Elite Soccer Academy - Children's Soccer Training Booking Platform
Flask application with Stripe + Venmo + Zelle payments, calendar with age-group slots,
parent accounts with messaging/reminders, and full coach admin dashboard.
"""

import os
import json
import uuid
import secrets
import hashlib
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, abort, send_file
)
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
import stripe
from icalendar import Calendar as ICSCalendar, Event as ICSEvent
import pytz

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///soccer_academy.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_placeholder')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_placeholder')

# Mail
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@elitesocceracademy.com')

BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')
INSTAGRAM_URL = os.environ.get('INSTAGRAM_URL', 'https://www.instagram.com/elitesocceracademy/')

# Venmo / Zelle coach info (set via env vars in production)
VENMO_HANDLE = os.environ.get('VENMO_HANDLE', '@CoachEliteSoccer')
ZELLE_EMAIL = os.environ.get('ZELLE_EMAIL', 'coach@elitesocceracademy.com')
ZELLE_PHONE = os.environ.get('ZELLE_PHONE', '(555) 123-4567')

db = SQLAlchemy(app)
mail = Mail(app)

# ─── Models ───────────────────────────────────────────────────────────────────

class Parent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50))
    password_hash = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    children = db.relationship('Child', backref='parent', lazy=True)
    stripe_customer_id = db.Column(db.String(255))

    def set_password(self, password):
        self.password_hash = hashlib.sha256((password + app.config['SECRET_KEY']).encode()).hexdigest()

    def check_password(self, password):
        if not self.password_hash:
            return False
        return self.password_hash == hashlib.sha256((password + app.config['SECRET_KEY']).encode()).hexdigest()

class Child(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('parent.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    enrollments = db.relationship('Enrollment', backref='child', lazy=True)

class Program(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text)
    age_min = db.Column(db.Integer)
    age_max = db.Column(db.Integer)
    max_capacity = db.Column(db.Integer)
    program_type = db.Column(db.String(50))  # 'group' or 'private'
    price_per_session = db.Column(db.Integer)  # cents
    price_monthly = db.Column(db.Integer)  # cents
    sibling_discount_pct = db.Column(db.Integer, default=10)
    color = db.Column(db.String(20), default='#2d6a4f')  # calendar color
    active = db.Column(db.Boolean, default=True)
    sessions = db.relationship('ClassSession', backref='program', lazy=True)

class ClassSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.Integer, db.ForeignKey('program.id'), nullable=False)
    title = db.Column(db.String(255))
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    location = db.Column(db.String(255), default='Main Training Field')
    notes = db.Column(db.Text)
    cancelled = db.Column(db.Boolean, default=False)
    season = db.Column(db.String(50))
    recurring_group = db.Column(db.String(100))  # for recurring weekly sessions
    enrollments = db.relationship('Enrollment', backref='class_session', lazy=True)
    attendance_records = db.relationship('Attendance', backref='class_session', lazy=True)

    @property
    def enrolled_count(self):
        return Enrollment.query.filter_by(session_id=self.id, status='confirmed').count()

    @property
    def waitlist_count(self):
        return Enrollment.query.filter_by(session_id=self.id, status='waitlisted').count()

    @property
    def is_full(self):
        if self.program and self.program.max_capacity:
            return self.enrolled_count >= self.program.max_capacity
        return False

    @property
    def spots_left(self):
        if self.program and self.program.max_capacity:
            return max(0, self.program.max_capacity - self.enrolled_count)
        return None

class Enrollment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('child.id'), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey('class_session.id'), nullable=False)
    status = db.Column(db.String(50), default='confirmed')  # confirmed, waitlisted, cancelled
    payment_status = db.Column(db.String(50), default='pending')  # pending, paid, refunded, pending_verification
    payment_method = db.Column(db.String(50), default='card')  # card, venmo, zelle
    stripe_payment_id = db.Column(db.String(255))
    venmo_transaction_id = db.Column(db.String(255))
    amount_paid = db.Column(db.Integer, default=0)  # cents
    payment_type = db.Column(db.String(50), default='per_session')  # per_session, monthly
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    cancelled_at = db.Column(db.DateTime)
    waitlist_position = db.Column(db.Integer)

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('parent.id'), nullable=False)
    enrollment_id = db.Column(db.Integer, db.ForeignKey('enrollment.id'))
    stripe_session_id = db.Column(db.String(255))
    stripe_payment_intent = db.Column(db.String(255))
    payment_method = db.Column(db.String(50), default='card')  # card, venmo, zelle
    transaction_ref = db.Column(db.String(255))  # venmo/zelle transaction ID
    amount = db.Column(db.Integer, nullable=False)  # cents
    description = db.Column(db.String(500))
    status = db.Column(db.String(50), default='pending')  # pending, paid, pending_verification, refunded
    payment_type = db.Column(db.String(50), default='per_session')  # per_session, monthly
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    verified_at = db.Column(db.DateTime)
    parent = db.relationship('Parent', backref='payments')

class MagicLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False)
    token = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used = db.Column(db.Boolean, default=False)

class ChatMessage(db.Model):
    """Bidirectional messaging between parents and coach."""
    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('parent.id'), nullable=False)
    sender = db.Column(db.String(50), nullable=False)  # 'parent' or 'coach'
    subject = db.Column(db.String(255))
    body = db.Column(db.Text, nullable=False)
    is_broadcast = db.Column(db.Boolean, default=False)
    read_by_parent = db.Column(db.Boolean, default=False)
    read_by_coach = db.Column(db.Boolean, default=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    parent = db.relationship('Parent', backref='chat_messages')

class Reminder(db.Model):
    """Tracks sent reminders."""
    id = db.Column(db.Integer, primary_key=True)
    enrollment_id = db.Column(db.Integer, db.ForeignKey('enrollment.id'), nullable=False)
    reminder_type = db.Column(db.String(50))  # '24hr', '1hr'
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    enrollment = db.relationship('Enrollment', backref='reminders')

class Attendance(db.Model):
    """Track attendance per session."""
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('class_session.id'), nullable=False)
    child_id = db.Column(db.Integer, db.ForeignKey('child.id'), nullable=False)
    present = db.Column(db.Boolean, default=False)
    marked_at = db.Column(db.DateTime, default=datetime.utcnow)
    child = db.relationship('Child', backref='attendance_records')

class RecurringSchedule(db.Model):
    """Recurring weekly schedule template."""
    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.Integer, db.ForeignKey('program.id'), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)  # 0=Mon, 6=Sun
    start_hour = db.Column(db.Integer, nullable=False)
    start_minute = db.Column(db.Integer, default=0)
    duration_minutes = db.Column(db.Integer, default=60)
    location = db.Column(db.String(255), default='Main Training Field')
    active = db.Column(db.Boolean, default=True)
    program = db.relationship('Program', backref='recurring_schedules')

class BlockedSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    reason = db.Column(db.String(255))

# ─── Auth helpers ─────────────────────────────────────────────────────────────

ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin@elitesocceracademy.com')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

def parent_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'parent_id' not in session:
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('parent_login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            flash('Admin access required.', 'danger')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

# ─── Context processors ──────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    unread_parent = 0
    if session.get('parent_id'):
        unread_parent = ChatMessage.query.filter_by(
            parent_id=session['parent_id'], sender='coach', read_by_parent=False
        ).count()
    unread_coach = 0
    if session.get('is_admin'):
        unread_coach = ChatMessage.query.filter_by(sender='parent', read_by_coach=False).count()
    pending_payments = 0
    if session.get('is_admin'):
        pending_payments = Payment.query.filter_by(status='pending_verification').count()
    return {
        'stripe_key': STRIPE_PUBLISHABLE_KEY,
        'instagram_url': INSTAGRAM_URL,
        'current_year': datetime.utcnow().year,
        'site_name': 'Elite Soccer Academy',
        'coach_name': 'Coach Alex Rivera',
        'venmo_handle': VENMO_HANDLE,
        'zelle_email': ZELLE_EMAIL,
        'zelle_phone': ZELLE_PHONE,
        'unread_parent_messages': unread_parent,
        'unread_coach_messages': unread_coach,
        'pending_payments_count': pending_payments,
    }

# ─── Public routes ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    programs = Program.query.filter_by(active=True).order_by(Program.age_min).all()
    return render_template('index.html', programs=programs)

@app.route('/programs')
def programs():
    programs = Program.query.filter_by(active=True).order_by(Program.age_min).all()
    return render_template('programs.html', programs=programs)

@app.route('/program/<slug>')
def program_detail(slug):
    program = Program.query.filter_by(slug=slug, active=True).first_or_404()
    upcoming = ClassSession.query.filter(
        ClassSession.program_id == program.id,
        ClassSession.start_time >= datetime.utcnow(),
        ClassSession.cancelled == False
    ).order_by(ClassSession.start_time).all()
    return render_template('program_detail.html', program=program, sessions=upcoming)

@app.route('/calendar')
def calendar_view():
    programs = Program.query.filter_by(active=True).order_by(Program.age_min).all()
    return render_template('calendar.html', programs=programs)

@app.route('/api/sessions')
def api_sessions():
    start = request.args.get('start')
    end = request.args.get('end')
    query = ClassSession.query.filter(ClassSession.cancelled == False)
    if start:
        query = query.filter(ClassSession.start_time >= start)
    if end:
        query = query.filter(ClassSession.end_time <= end)
    sessions = query.all()
    events = []

    # Color map per program slug
    color_map = {
        'little-kicks': '#52b788',
        'junior-stars': '#2196F3',
        'skill-builders': '#FF9800',
        'rising-players': '#E91E63',
        'elite-youth': '#F44336',
        'one-on-one': '#9C27B0',
    }

    for s in sessions:
        color = '#2d6a4f'
        if s.program:
            color = color_map.get(s.program.slug, s.program.color or '#2d6a4f')

        title = s.title or (s.program.name if s.program else 'Session')
        spots = s.spots_left
        cap = s.program.max_capacity if s.program else None
        enrolled = s.enrolled_count

        if cap is not None:
            title += f' ({enrolled}/{cap})'

        # Determine status color class
        status = 'available'
        if s.is_full:
            status = 'full'
        elif spots is not None and spots <= 2:
            status = 'almost-full'

        events.append({
            'id': s.id,
            'title': title,
            'start': s.start_time.isoformat(),
            'end': s.end_time.isoformat(),
            'color': color if status != 'full' else '#9e9e9e',
            'borderColor': color,
            'url': url_for('book_session', session_id=s.id),
            'extendedProps': {
                'location': s.location,
                'enrolled': enrolled,
                'capacity': cap,
                'spotsLeft': spots,
                'isFull': s.is_full,
                'status': status,
                'programName': s.program.name if s.program else '',
                'programSlug': s.program.slug if s.program else '',
                'time': s.start_time.strftime('%I:%M %p'),
            }
        })

    # Add blocked slots
    blocked = BlockedSlot.query.all()
    for b in blocked:
        events.append({
            'id': f'blocked-{b.id}',
            'title': 'Unavailable',
            'start': b.start_time.isoformat(),
            'end': b.end_time.isoformat(),
            'color': '#6c757d',
            'display': 'background'
        })
    return jsonify(events)

@app.route('/gallery')
def gallery():
    return render_template('gallery.html')

# ─── Booking flow ─────────────────────────────────────────────────────────────

@app.route('/book/<int:session_id>', methods=['GET', 'POST'])
def book_session(session_id):
    sess = ClassSession.query.get_or_404(session_id)
    if sess.cancelled:
        flash('This session has been cancelled.', 'danger')
        return redirect(url_for('calendar_view'))

    # Pre-fill from parent session if logged in
    logged_parent = None
    if session.get('parent_id'):
        logged_parent = Parent.query.get(session['parent_id'])

    if request.method == 'POST':
        parent_email = request.form.get('parent_email', '').strip().lower()
        parent_name = request.form.get('parent_name', '').strip()
        parent_phone = request.form.get('parent_phone', '').strip()
        child_name = request.form.get('child_name', '').strip()
        child_age = int(request.form.get('child_age', 0))
        payment_method = request.form.get('payment_method', 'card')
        payment_type = request.form.get('payment_type', 'per_session')

        if not all([parent_email, parent_name, child_name, child_age]):
            flash('Please fill in all required fields.', 'danger')
            return render_template('book_session.html', session=sess, parent=logged_parent)

        # Age validation
        if sess.program.age_min and child_age < sess.program.age_min:
            flash(f'This program requires a minimum age of {sess.program.age_min}.', 'danger')
            return render_template('book_session.html', session=sess, parent=logged_parent)
        if sess.program.age_max and child_age > sess.program.age_max:
            flash(f'This program is for ages up to {sess.program.age_max}.', 'danger')
            return render_template('book_session.html', session=sess, parent=logged_parent)

        # Find or create parent
        parent = Parent.query.filter_by(email=parent_email).first()
        if not parent:
            parent = Parent(email=parent_email, name=parent_name, phone=parent_phone)
            db.session.add(parent)
            db.session.flush()
        elif parent_phone and not parent.phone:
            parent.phone = parent_phone

        # Find or create child
        child = Child.query.filter_by(parent_id=parent.id, name=child_name).first()
        if not child:
            child = Child(parent_id=parent.id, name=child_name, age=child_age)
            db.session.add(child)
            db.session.flush()
        else:
            child.age = child_age

        # Check duplicate enrollment
        existing = Enrollment.query.filter_by(
            child_id=child.id, session_id=sess.id
        ).filter(Enrollment.status != 'cancelled').first()
        if existing:
            flash('This child is already enrolled in this session.', 'warning')
            return render_template('book_session.html', session=sess, parent=logged_parent)

        # Determine status
        status = 'waitlisted' if sess.is_full else 'confirmed'
        waitlist_pos = None
        if status == 'waitlisted':
            waitlist_pos = sess.waitlist_count + 1

        # Calculate price with sibling discount
        if payment_type == 'monthly':
            price = sess.program.price_monthly or sess.program.price_per_session or 0
        else:
            price = sess.program.price_per_session or 0

        sibling_count = Enrollment.query.join(Child).filter(
            Child.parent_id == parent.id,
            Enrollment.session_id == sess.id,
            Enrollment.status == 'confirmed'
        ).count()
        if sibling_count > 0 and sess.program.sibling_discount_pct:
            discount = price * sess.program.sibling_discount_pct / 100
            price = int(price - discount)

        enrollment = Enrollment(
            child_id=child.id,
            session_id=sess.id,
            status=status,
            waitlist_position=waitlist_pos,
            amount_paid=0,
            payment_method=payment_method,
            payment_type=payment_type,
        )
        db.session.add(enrollment)
        db.session.commit()

        if status == 'waitlisted':
            flash(f'{child_name} has been added to the waitlist (position #{waitlist_pos}).', 'info')
            return redirect(url_for('booking_confirmation', enrollment_id=enrollment.id))

        # Route to appropriate payment method
        if price > 0:
            if payment_method == 'card':
                return redirect(url_for('checkout', enrollment_id=enrollment.id, payment_type=payment_type))
            elif payment_method in ('venmo', 'zelle'):
                return redirect(url_for('manual_payment', enrollment_id=enrollment.id))
        else:
            enrollment.payment_status = 'paid'
            db.session.commit()
            flash('Booking confirmed!', 'success')
            return redirect(url_for('booking_confirmation', enrollment_id=enrollment.id))

    return render_template('book_session.html', session=sess, parent=logged_parent)

@app.route('/checkout/<int:enrollment_id>')
def checkout(enrollment_id):
    enrollment = Enrollment.query.get_or_404(enrollment_id)
    sess = enrollment.class_session
    child = enrollment.child
    program = sess.program
    payment_type = request.args.get('payment_type', enrollment.payment_type or 'per_session')

    if payment_type == 'monthly':
        price = program.price_monthly or program.price_per_session or 0
    else:
        price = program.price_per_session or 0

    # Sibling discount
    sibling_count = Enrollment.query.join(Child).filter(
        Child.parent_id == child.parent_id,
        Enrollment.session_id == sess.id,
        Enrollment.status == 'confirmed',
        Enrollment.id != enrollment.id
    ).count()
    if sibling_count > 0 and program.sibling_discount_pct:
        discount = price * program.sibling_discount_pct / 100
        price = int(price - discount)

    try:
        price_label = 'Monthly Package' if payment_type == 'monthly' else 'Per Session'
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'{program.name} ({price_label}) - {sess.start_time.strftime("%b %d, %Y %I:%M %p")}',
                        'description': f'Student: {child.name}',
                    },
                    'unit_amount': price,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=BASE_URL + url_for('payment_success', enrollment_id=enrollment.id) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=BASE_URL + url_for('payment_cancel', enrollment_id=enrollment.id),
            metadata={
                'enrollment_id': enrollment.id,
                'child_name': child.name,
                'parent_id': child.parent_id,
            }
        )
        payment = Payment(
            parent_id=child.parent_id,
            enrollment_id=enrollment.id,
            stripe_session_id=checkout_session.id,
            amount=price,
            description=f'{program.name} ({price_label}) - {child.name}',
            status='pending',
            payment_method='card',
            payment_type=payment_type,
        )
        db.session.add(payment)
        db.session.commit()
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        flash(f'Payment error: {str(e)}', 'danger')
        return redirect(url_for('book_session', session_id=sess.id))

@app.route('/payment/manual/<int:enrollment_id>', methods=['GET', 'POST'])
def manual_payment(enrollment_id):
    enrollment = Enrollment.query.get_or_404(enrollment_id)
    sess = enrollment.class_session
    child = enrollment.child
    program = sess.program
    method = enrollment.payment_method

    if enrollment.payment_type == 'monthly':
        price = program.price_monthly or program.price_per_session or 0
    else:
        price = program.price_per_session or 0

    # Sibling discount
    sibling_count = Enrollment.query.join(Child).filter(
        Child.parent_id == child.parent_id,
        Enrollment.session_id == sess.id,
        Enrollment.status == 'confirmed',
        Enrollment.id != enrollment.id
    ).count()
    if sibling_count > 0 and program.sibling_discount_pct:
        discount = price * program.sibling_discount_pct / 100
        price = int(price - discount)

    if request.method == 'POST':
        transaction_ref = request.form.get('transaction_ref', '').strip()
        if not transaction_ref:
            flash('Please enter the transaction ID / confirmation number.', 'danger')
            return render_template('manual_payment.html', enrollment=enrollment, price=price, method=method)

        enrollment.payment_status = 'pending_verification'
        enrollment.venmo_transaction_id = transaction_ref
        enrollment.amount_paid = price

        payment = Payment(
            parent_id=child.parent_id,
            enrollment_id=enrollment.id,
            amount=price,
            description=f'{program.name} ({enrollment.payment_type}) - {child.name}',
            status='pending_verification',
            payment_method=method,
            transaction_ref=transaction_ref,
            payment_type=enrollment.payment_type,
        )
        db.session.add(payment)
        db.session.commit()
        flash(f'Payment submitted via {method.title()}! Awaiting coach verification.', 'info')
        return redirect(url_for('booking_confirmation', enrollment_id=enrollment.id))

    return render_template('manual_payment.html', enrollment=enrollment, price=price, method=method)

@app.route('/payment/success/<int:enrollment_id>')
def payment_success(enrollment_id):
    enrollment = Enrollment.query.get_or_404(enrollment_id)
    stripe_session_id = request.args.get('session_id')
    if stripe_session_id:
        payment = Payment.query.filter_by(stripe_session_id=stripe_session_id).first()
        if payment:
            payment.status = 'paid'
            enrollment.payment_status = 'paid'
            enrollment.amount_paid = payment.amount
            db.session.commit()
    flash('Payment successful! Your booking is confirmed.', 'success')
    return redirect(url_for('booking_confirmation', enrollment_id=enrollment_id))

@app.route('/payment/cancel/<int:enrollment_id>')
def payment_cancel(enrollment_id):
    flash('Payment was cancelled. Your spot is reserved for 15 minutes.', 'warning')
    return redirect(url_for('book_session', session_id=Enrollment.query.get(enrollment_id).session_id))

@app.route('/booking/confirmation/<int:enrollment_id>')
def booking_confirmation(enrollment_id):
    enrollment = Enrollment.query.get_or_404(enrollment_id)
    return render_template('booking_confirmation.html', enrollment=enrollment)

@app.route('/booking/<int:enrollment_id>/ics')
def download_ics(enrollment_id):
    enrollment = Enrollment.query.get_or_404(enrollment_id)
    sess = enrollment.class_session
    cal = ICSCalendar()
    cal.add('prodid', '-//Elite Soccer Academy//EN')
    cal.add('version', '2.0')
    event = ICSEvent()
    event.add('summary', f'{sess.program.name} - {enrollment.child.name}')
    event.add('dtstart', sess.start_time)
    event.add('dtend', sess.end_time)
    event.add('location', sess.location)
    event.add('description', f'Soccer training session for {enrollment.child.name}')
    event.add('uid', f'{enrollment.id}@elitesocceracademy.com')
    cal.add_component(event)

    response = app.make_response(cal.to_ical())
    response.headers['Content-Type'] = 'text/calendar'
    response.headers['Content-Disposition'] = f'attachment; filename=soccer_session_{enrollment.id}.ics'
    return response

# ─── Parent portal ────────────────────────────────────────────────────────────

@app.route('/parent/register', methods=['GET', 'POST'])
def parent_register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        if not all([name, email, password]):
            flash('Please fill in all required fields.', 'danger')
            return render_template('parent_register.html')

        if password != confirm:
            flash('Passwords do not match.', 'danger')
            return render_template('parent_register.html')

        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
            return render_template('parent_register.html')

        existing = Parent.query.filter_by(email=email).first()
        if existing:
            if existing.password_hash:
                flash('An account with this email already exists. Please log in.', 'warning')
                return redirect(url_for('parent_login'))
            else:
                # Existing parent from booking, set password
                existing.set_password(password)
                existing.name = name
                if phone:
                    existing.phone = phone
                db.session.commit()
                session['parent_id'] = existing.id
                flash('Account activated! Welcome.', 'success')
                return redirect(url_for('parent_dashboard'))

        parent = Parent(email=email, name=name, phone=phone)
        parent.set_password(password)
        db.session.add(parent)
        db.session.commit()
        session['parent_id'] = parent.id
        flash('Account created! Welcome to Elite Soccer Academy.', 'success')
        return redirect(url_for('parent_dashboard'))

    return render_template('parent_register.html')

@app.route('/parent/login', methods=['GET', 'POST'])
def parent_login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        parent = Parent.query.filter_by(email=email).first()
        if parent and parent.check_password(password):
            session['parent_id'] = parent.id
            flash('Welcome back!', 'success')
            return redirect(url_for('parent_dashboard'))
        elif parent and not parent.password_hash:
            flash('Please register to set up your password.', 'info')
            return redirect(url_for('parent_register'))
        else:
            flash('Invalid email or password.', 'danger')

    return render_template('parent_login.html')

@app.route('/parent/dashboard')
@parent_required
def parent_dashboard():
    parent = Parent.query.get(session['parent_id'])
    children = Child.query.filter_by(parent_id=parent.id).all()
    enrollments = Enrollment.query.join(Child).filter(
        Child.parent_id == parent.id
    ).order_by(Enrollment.created_at.desc()).all()
    payments = Payment.query.filter_by(parent_id=parent.id).order_by(Payment.created_at.desc()).all()

    # Upcoming sessions (for reminders display)
    upcoming = []
    for e in enrollments:
        if e.status == 'confirmed' and e.class_session.start_time > datetime.utcnow():
            upcoming.append(e)

    # Unread messages
    unread = ChatMessage.query.filter_by(
        parent_id=parent.id, sender='coach', read_by_parent=False
    ).count()

    return render_template('parent_dashboard.html', parent=parent, children=children,
                           enrollments=enrollments, payments=payments,
                           upcoming=upcoming, unread_messages=unread)

@app.route('/parent/child/add', methods=['POST'])
@parent_required
def parent_add_child():
    name = request.form.get('child_name', '').strip()
    age = request.form.get('child_age', 0, type=int)
    notes = request.form.get('notes', '').strip()
    if not name or not age:
        flash('Please provide child name and age.', 'danger')
        return redirect(url_for('parent_dashboard'))

    existing = Child.query.filter_by(parent_id=session['parent_id'], name=name).first()
    if existing:
        flash(f'{name} is already registered.', 'warning')
        return redirect(url_for('parent_dashboard'))

    child = Child(parent_id=session['parent_id'], name=name, age=age, notes=notes)
    db.session.add(child)
    db.session.commit()
    flash(f'{name} has been added.', 'success')
    return redirect(url_for('parent_dashboard'))

@app.route('/parent/cancel/<int:enrollment_id>', methods=['POST'])
@parent_required
def parent_cancel(enrollment_id):
    enrollment = Enrollment.query.get_or_404(enrollment_id)
    if enrollment.child.parent_id != session['parent_id']:
        abort(403)
    sess = enrollment.class_session
    hours_until = (sess.start_time - datetime.utcnow()).total_seconds() / 3600
    if hours_until < 48:
        flash('Cancellations must be made at least 48 hours in advance.', 'danger')
        return redirect(url_for('parent_dashboard'))
    enrollment.status = 'cancelled'
    enrollment.cancelled_at = datetime.utcnow()
    db.session.commit()
    # Promote from waitlist
    next_waitlisted = Enrollment.query.filter_by(
        session_id=sess.id, status='waitlisted'
    ).order_by(Enrollment.waitlist_position).first()
    if next_waitlisted:
        next_waitlisted.status = 'confirmed'
        next_waitlisted.waitlist_position = None
        db.session.commit()
    flash('Booking cancelled successfully.', 'success')
    return redirect(url_for('parent_dashboard'))

@app.route('/parent/messages', methods=['GET', 'POST'])
@parent_required
def parent_messages():
    parent = Parent.query.get(session['parent_id'])
    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        subject = request.form.get('subject', '').strip()
        if body:
            msg = ChatMessage(
                parent_id=parent.id,
                sender='parent',
                subject=subject,
                body=body,
                read_by_parent=True,
                read_by_coach=False,
            )
            db.session.add(msg)
            db.session.commit()
            flash('Message sent to coach.', 'success')
        return redirect(url_for('parent_messages'))

    # Mark coach messages as read
    unread = ChatMessage.query.filter_by(
        parent_id=parent.id, sender='coach', read_by_parent=False
    ).all()
    for m in unread:
        m.read_by_parent = True
    db.session.commit()

    messages = ChatMessage.query.filter_by(parent_id=parent.id).order_by(ChatMessage.sent_at.asc()).all()
    return render_template('parent_messages.html', parent=parent, messages=messages)

@app.route('/parent/reminders')
@parent_required
def parent_reminders():
    parent = Parent.query.get(session['parent_id'])
    # Get all reminders for this parent's children
    reminders = Reminder.query.join(Enrollment).join(Child).filter(
        Child.parent_id == parent.id
    ).order_by(Reminder.sent_at.desc()).limit(50).all()
    return render_template('parent_reminders.html', parent=parent, reminders=reminders)

@app.route('/parent/logout')
def parent_logout():
    session.pop('parent_id', None)
    flash('Logged out.', 'info')
    return redirect(url_for('index'))

# ─── Reminder check endpoint (call via cron or scheduler) ────────────────────

@app.route('/api/check-reminders')
def check_reminders():
    """Check and send reminders. Call this endpoint periodically."""
    now = datetime.utcnow()
    results = {'24hr': 0, '1hr': 0}

    for reminder_type, delta in [('24hr', timedelta(hours=24)), ('1hr', timedelta(hours=1))]:
        window_start = now + delta - timedelta(minutes=15)
        window_end = now + delta + timedelta(minutes=15)

        enrollments = Enrollment.query.join(ClassSession).filter(
            Enrollment.status == 'confirmed',
            ClassSession.start_time >= window_start,
            ClassSession.start_time <= window_end,
            ClassSession.cancelled == False,
        ).all()

        for e in enrollments:
            existing = Reminder.query.filter_by(
                enrollment_id=e.id, reminder_type=reminder_type
            ).first()
            if not existing:
                reminder = Reminder(enrollment_id=e.id, reminder_type=reminder_type)
                db.session.add(reminder)
                results[reminder_type] += 1

    db.session.commit()
    return jsonify(results)

# ─── Admin ────────────────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        email = request.form.get('email', '')
        password = request.form.get('password', '')
        if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            session['is_admin'] = True
            flash('Welcome, Coach!', 'success')
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials.', 'danger')
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    flash('Logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    total_enrolled = Enrollment.query.filter_by(status='confirmed').count()
    total_waitlisted = Enrollment.query.filter_by(status='waitlisted').count()
    total_parents = Parent.query.count()
    total_revenue = db.session.query(db.func.sum(Payment.amount)).filter(
        Payment.status.in_(['paid'])
    ).scalar() or 0
    pending_payments = Payment.query.filter_by(status='pending_verification').count()
    upcoming_sessions = ClassSession.query.filter(
        ClassSession.start_time >= datetime.utcnow(),
        ClassSession.cancelled == False
    ).order_by(ClassSession.start_time).limit(10).all()
    recent_enrollments = Enrollment.query.order_by(Enrollment.created_at.desc()).limit(10).all()
    unread_messages = ChatMessage.query.filter_by(sender='parent', read_by_coach=False).count()
    return render_template('admin_dashboard.html',
                           total_enrolled=total_enrolled,
                           total_waitlisted=total_waitlisted,
                           total_parents=total_parents,
                           total_revenue=total_revenue,
                           pending_payments=pending_payments,
                           upcoming_sessions=upcoming_sessions,
                           recent_enrollments=recent_enrollments,
                           unread_messages=unread_messages)

@app.route('/admin/programs')
@admin_required
def admin_programs():
    programs = Program.query.all()
    return render_template('admin_programs.html', programs=programs)

@app.route('/admin/program/new', methods=['GET', 'POST'])
@admin_required
def admin_program_new():
    if request.method == 'POST':
        program = Program(
            name=request.form['name'],
            slug=request.form['slug'],
            description=request.form.get('description', ''),
            age_min=int(request.form.get('age_min', 0)) or None,
            age_max=int(request.form.get('age_max', 0)) or None,
            max_capacity=int(request.form.get('max_capacity', 0)) or None,
            program_type=request.form.get('program_type', 'group'),
            price_per_session=int(float(request.form.get('price_per_session', 0)) * 100),
            price_monthly=int(float(request.form.get('price_monthly', 0)) * 100),
            sibling_discount_pct=int(request.form.get('sibling_discount_pct', 10)),
            color=request.form.get('color', '#2d6a4f'),
        )
        db.session.add(program)
        db.session.commit()
        flash('Program created.', 'success')
        return redirect(url_for('admin_programs'))
    return render_template('admin_program_form.html', program=None)

@app.route('/admin/program/<int:program_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_program_edit(program_id):
    program = Program.query.get_or_404(program_id)
    if request.method == 'POST':
        program.name = request.form['name']
        program.slug = request.form['slug']
        program.description = request.form.get('description', '')
        program.age_min = int(request.form.get('age_min', 0)) or None
        program.age_max = int(request.form.get('age_max', 0)) or None
        program.max_capacity = int(request.form.get('max_capacity', 0)) or None
        program.program_type = request.form.get('program_type', 'group')
        program.price_per_session = int(float(request.form.get('price_per_session', 0)) * 100)
        program.price_monthly = int(float(request.form.get('price_monthly', 0)) * 100)
        program.sibling_discount_pct = int(request.form.get('sibling_discount_pct', 10))
        program.color = request.form.get('color', '#2d6a4f')
        db.session.commit()
        flash('Program updated.', 'success')
        return redirect(url_for('admin_programs'))
    return render_template('admin_program_form.html', program=program)

@app.route('/admin/sessions')
@admin_required
def admin_sessions():
    sessions = ClassSession.query.order_by(ClassSession.start_time.desc()).all()
    programs = Program.query.filter_by(active=True).all()
    recurring = RecurringSchedule.query.all()
    return render_template('admin_sessions.html', sessions=sessions, programs=programs, recurring=recurring)

@app.route('/admin/session/new', methods=['POST'])
@admin_required
def admin_session_new():
    sess = ClassSession(
        program_id=int(request.form['program_id']),
        title=request.form.get('title', ''),
        start_time=datetime.strptime(request.form['start_time'], '%Y-%m-%dT%H:%M'),
        end_time=datetime.strptime(request.form['end_time'], '%Y-%m-%dT%H:%M'),
        location=request.form.get('location', 'Main Training Field'),
        season=request.form.get('season', ''),
        notes=request.form.get('notes', '')
    )
    db.session.add(sess)
    db.session.commit()
    flash('Session created.', 'success')
    return redirect(url_for('admin_sessions'))

@app.route('/admin/session/<int:session_id>/cancel', methods=['POST'])
@admin_required
def admin_session_cancel(session_id):
    sess = ClassSession.query.get_or_404(session_id)
    sess.cancelled = True
    enrollments = Enrollment.query.filter_by(session_id=session_id, status='confirmed').all()
    for e in enrollments:
        e.status = 'cancelled'
    db.session.commit()
    flash(f'Session cancelled. {len(enrollments)} enrollments affected.', 'warning')
    return redirect(url_for('admin_sessions'))

@app.route('/admin/recurring/new', methods=['POST'])
@admin_required
def admin_recurring_new():
    rs = RecurringSchedule(
        program_id=int(request.form['program_id']),
        day_of_week=int(request.form['day_of_week']),
        start_hour=int(request.form['start_hour']),
        start_minute=int(request.form.get('start_minute', 0)),
        duration_minutes=int(request.form.get('duration_minutes', 60)),
        location=request.form.get('location', 'Main Training Field'),
    )
    db.session.add(rs)
    db.session.commit()
    flash('Recurring schedule added.', 'success')
    return redirect(url_for('admin_sessions'))

@app.route('/admin/recurring/<int:rs_id>/delete', methods=['POST'])
@admin_required
def admin_recurring_delete(rs_id):
    rs = RecurringSchedule.query.get_or_404(rs_id)
    db.session.delete(rs)
    db.session.commit()
    flash('Recurring schedule removed.', 'success')
    return redirect(url_for('admin_sessions'))

@app.route('/admin/generate-sessions', methods=['POST'])
@admin_required
def admin_generate_sessions():
    """Generate sessions from recurring schedules for the next N weeks."""
    weeks = int(request.form.get('weeks', 4))
    recurring = RecurringSchedule.query.filter_by(active=True).all()
    count = 0
    base = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    for rs in recurring:
        for week in range(weeks):
            # Find the next occurrence of this day
            days_ahead = rs.day_of_week - base.weekday()
            if days_ahead < 0:
                days_ahead += 7
            session_date = base + timedelta(days=days_ahead + week * 7)
            start = session_date.replace(hour=rs.start_hour, minute=rs.start_minute)
            end = start + timedelta(minutes=rs.duration_minutes)

            if start <= datetime.utcnow():
                continue

            # Check for duplicates
            existing = ClassSession.query.filter_by(
                program_id=rs.program_id,
                start_time=start
            ).first()
            if not existing:
                sess = ClassSession(
                    program_id=rs.program_id,
                    title=rs.program.name,
                    start_time=start,
                    end_time=end,
                    location=rs.location,
                    recurring_group=f'recurring-{rs.id}'
                )
                db.session.add(sess)
                count += 1

    db.session.commit()
    flash(f'{count} sessions generated for the next {weeks} weeks.', 'success')
    return redirect(url_for('admin_sessions'))

@app.route('/admin/enrollments')
@admin_required
def admin_enrollments():
    enrollments = Enrollment.query.order_by(Enrollment.created_at.desc()).all()
    return render_template('admin_enrollments.html', enrollments=enrollments)

@app.route('/admin/enrollment/<int:enrollment_id>/details')
@admin_required
def admin_enrollment_details(enrollment_id):
    """View enrollment with parent contact info."""
    enrollment = Enrollment.query.get_or_404(enrollment_id)
    return render_template('admin_enrollment_details.html', enrollment=enrollment)

@app.route('/admin/block-time', methods=['POST'])
@admin_required
def admin_block_time():
    blocked = BlockedSlot(
        start_time=datetime.strptime(request.form['start_time'], '%Y-%m-%dT%H:%M'),
        end_time=datetime.strptime(request.form['end_time'], '%Y-%m-%dT%H:%M'),
        reason=request.form.get('reason', '')
    )
    db.session.add(blocked)
    db.session.commit()
    flash('Time blocked.', 'success')
    return redirect(url_for('admin_sessions'))

# ─── Admin payments ──────────────────────────────────────────────────────────

@app.route('/admin/payments')
@admin_required
def admin_payments():
    pending = Payment.query.filter_by(status='pending_verification').order_by(Payment.created_at.desc()).all()
    all_payments = Payment.query.order_by(Payment.created_at.desc()).limit(100).all()
    return render_template('admin_payments.html', pending=pending, all_payments=all_payments)

@app.route('/admin/payment/<int:payment_id>/verify', methods=['POST'])
@admin_required
def admin_verify_payment(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    payment.status = 'paid'
    payment.verified_at = datetime.utcnow()
    if payment.enrollment_id:
        enrollment = Enrollment.query.get(payment.enrollment_id)
        if enrollment:
            enrollment.payment_status = 'paid'
    db.session.commit()
    flash(f'Payment verified for ${payment.amount/100:.2f} via {payment.payment_method.title()}.', 'success')
    return redirect(url_for('admin_payments'))

@app.route('/admin/payment/<int:payment_id>/reject', methods=['POST'])
@admin_required
def admin_reject_payment(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    payment.status = 'rejected'
    if payment.enrollment_id:
        enrollment = Enrollment.query.get(payment.enrollment_id)
        if enrollment:
            enrollment.payment_status = 'pending'
    db.session.commit()
    flash('Payment rejected.', 'warning')
    return redirect(url_for('admin_payments'))

# ─── Admin messaging ─────────────────────────────────────────────────────────

@app.route('/admin/messages')
@admin_required
def admin_message():
    # Get parents with conversation threads
    parents = Parent.query.order_by(Parent.name).all()
    parent_id = request.args.get('parent_id', type=int)
    messages = []
    selected_parent = None

    if parent_id:
        selected_parent = Parent.query.get(parent_id)
        messages = ChatMessage.query.filter_by(parent_id=parent_id).order_by(ChatMessage.sent_at.asc()).all()
        # Mark parent messages as read
        for m in messages:
            if m.sender == 'parent':
                m.read_by_coach = True
        db.session.commit()

    # Get parents with unread messages
    parents_with_unread = {}
    for p in parents:
        count = ChatMessage.query.filter_by(parent_id=p.id, sender='parent', read_by_coach=False).count()
        parents_with_unread[p.id] = count

    return render_template('admin_messages.html', parents=parents, messages=messages,
                           selected_parent=selected_parent, parents_with_unread=parents_with_unread)

@app.route('/admin/message/reply', methods=['POST'])
@admin_required
def admin_message_reply():
    parent_id = int(request.form['parent_id'])
    body = request.form.get('body', '').strip()
    subject = request.form.get('subject', '').strip()
    if body:
        msg = ChatMessage(
            parent_id=parent_id,
            sender='coach',
            subject=subject,
            body=body,
            read_by_coach=True,
            read_by_parent=False,
        )
        db.session.add(msg)
        db.session.commit()
        flash('Reply sent.', 'success')
    return redirect(url_for('admin_message', parent_id=parent_id))

@app.route('/admin/broadcast', methods=['GET', 'POST'])
@admin_required
def admin_broadcast():
    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        subject = request.form.get('subject', '').strip()
        target = request.form.get('target', 'all')  # all, program-slug, session-id

        parents_to_notify = set()

        if target == 'all':
            parents_to_notify = set(p.id for p in Parent.query.all())
        elif target.startswith('program-'):
            slug = target.replace('program-', '')
            program = Program.query.filter_by(slug=slug).first()
            if program:
                enrollments = Enrollment.query.join(ClassSession).join(Child).filter(
                    ClassSession.program_id == program.id,
                    Enrollment.status.in_(['confirmed', 'waitlisted'])
                ).all()
                for e in enrollments:
                    parents_to_notify.add(e.child.parent_id)

        for pid in parents_to_notify:
            msg = ChatMessage(
                parent_id=pid,
                sender='coach',
                subject=subject or 'Announcement',
                body=body,
                is_broadcast=True,
                read_by_coach=True,
                read_by_parent=False,
            )
            db.session.add(msg)

        db.session.commit()
        flash(f'Broadcast sent to {len(parents_to_notify)} parents.', 'success')
        return redirect(url_for('admin_broadcast'))

    programs = Program.query.filter_by(active=True).all()
    return render_template('admin_broadcast.html', programs=programs)

# ─── Admin attendance ─────────────────────────────────────────────────────────

@app.route('/admin/attendance/<int:session_id>', methods=['GET', 'POST'])
@admin_required
def admin_attendance(session_id):
    sess = ClassSession.query.get_or_404(session_id)
    enrollments = Enrollment.query.filter_by(session_id=session_id, status='confirmed').all()

    if request.method == 'POST':
        # Clear old attendance for this session
        Attendance.query.filter_by(session_id=session_id).delete()
        for e in enrollments:
            present = request.form.get(f'present_{e.child_id}') == 'on'
            att = Attendance(session_id=session_id, child_id=e.child_id, present=present)
            db.session.add(att)
        db.session.commit()
        flash('Attendance saved.', 'success')
        return redirect(url_for('admin_attendance', session_id=session_id))

    # Get existing attendance
    attendance_map = {}
    for att in Attendance.query.filter_by(session_id=session_id).all():
        attendance_map[att.child_id] = att.present

    return render_template('admin_attendance.html', session=sess, enrollments=enrollments,
                           attendance_map=attendance_map)

# ─── Admin waitlist management ───────────────────────────────────────────────

@app.route('/admin/waitlist/<int:session_id>')
@admin_required
def admin_waitlist(session_id):
    sess = ClassSession.query.get_or_404(session_id)
    waitlisted = Enrollment.query.filter_by(
        session_id=session_id, status='waitlisted'
    ).order_by(Enrollment.waitlist_position).all()
    return render_template('admin_waitlist.html', session=sess, waitlisted=waitlisted)

@app.route('/admin/waitlist/promote/<int:enrollment_id>', methods=['POST'])
@admin_required
def admin_waitlist_promote(enrollment_id):
    enrollment = Enrollment.query.get_or_404(enrollment_id)
    enrollment.status = 'confirmed'
    enrollment.waitlist_position = None
    db.session.commit()
    flash(f'{enrollment.child.name} promoted from waitlist.', 'success')
    return redirect(url_for('admin_waitlist', session_id=enrollment.session_id))

@app.route('/admin/reports')
@admin_required
def admin_reports():
    payments = Payment.query.filter_by(status='paid').all()
    monthly = {}
    for p in payments:
        key = p.created_at.strftime('%Y-%m')
        monthly[key] = monthly.get(key, 0) + p.amount
    programs = Program.query.all()
    program_stats = []
    for prog in programs:
        enrolled = Enrollment.query.join(ClassSession).filter(
            ClassSession.program_id == prog.id,
            Enrollment.status == 'confirmed'
        ).count()
        program_stats.append({'name': prog.name, 'enrolled': enrolled})
    return render_template('admin_reports.html', monthly=monthly, program_stats=program_stats)

# ─── Stripe webhook ──────────────────────────────────────────────────────────

@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
    try:
        if endpoint_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
        else:
            event = json.loads(payload)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if event.get('type') == 'checkout.session.completed':
        sess = event['data']['object']
        payment = Payment.query.filter_by(stripe_session_id=sess['id']).first()
        if payment:
            payment.status = 'paid'
            payment.stripe_payment_intent = sess.get('payment_intent')
            if payment.enrollment_id:
                enrollment = Enrollment.query.get(payment.enrollment_id)
                if enrollment:
                    enrollment.payment_status = 'paid'
                    enrollment.amount_paid = payment.amount
            db.session.commit()
    return jsonify({'status': 'ok'})

# ─── Database seed ────────────────────────────────────────────────────────────

def seed_database():
    """Create default programs and sample sessions."""
    if Program.query.first():
        return

    programs_data = [
        {
            'name': 'Little Kicks (U6)',
            'slug': 'little-kicks',
            'description': 'A playful introduction to the world of soccer for our youngest athletes. Through age-appropriate games, obstacle courses, and mini-matches, children develop coordination, balance, spatial awareness, and social skills. Every session ends with a fun scrimmage and every player earns recognition for effort and sportsmanship. No experience necessary -- just enthusiasm!',
            'age_min': 4, 'age_max': 6, 'max_capacity': 8,
            'program_type': 'group',
            'price_per_session': 3500, 'price_monthly': 11900,
            'sibling_discount_pct': 15,
            'color': '#52b788',
        },
        {
            'name': 'Junior Stars (U8)',
            'slug': 'junior-stars',
            'description': 'Designed for players ready to build a strong technical foundation. Sessions focus on first-touch control, accurate passing, basic dribbling moves, and introductory shooting technique. Small-sided 4v4 and 5v5 games develop game awareness, decision-making, and creativity on the ball. Players begin to understand positions and teamwork concepts.',
            'age_min': 7, 'age_max': 8, 'max_capacity': 10,
            'program_type': 'group',
            'price_per_session': 4000, 'price_monthly': 13900,
            'sibling_discount_pct': 10,
            'color': '#2196F3',
        },
        {
            'name': 'Skill Builders (U10)',
            'slug': 'skill-builders',
            'description': 'An intermediate program bridging foundational skills with competitive play. Players refine dribbling under pressure, learn combination passing, practice crossing and finishing, and are introduced to set-piece execution. 7v7 match play with coaching intervention teaches reading the game, transition play, and communication on the pitch.',
            'age_min': 9, 'age_max': 10, 'max_capacity': 12,
            'program_type': 'group',
            'price_per_session': 4500, 'price_monthly': 15900,
            'sibling_discount_pct': 10,
            'color': '#FF9800',
        },
        {
            'name': 'Rising Players (U12)',
            'slug': 'rising-players',
            'description': 'Advanced skill development and tactical understanding for competitive-minded players. Position-specific training covers attacking movement, defensive shape, midfield link-up play, and goalkeeper fundamentals. Players work on set pieces, 9v9 match scenarios, and video analysis sessions to prepare for travel team and select squad tryouts.',
            'age_min': 11, 'age_max': 12, 'max_capacity': 14,
            'program_type': 'group',
            'price_per_session': 5000, 'price_monthly': 17900,
            'sibling_discount_pct': 10,
            'color': '#E91E63',
        },
        {
            'name': 'Elite Youth (U14)',
            'slug': 'elite-youth',
            'description': 'High-intensity training for serious players aiming for club, high school, and ODP-level competition. Sessions cover advanced tactical systems, fitness and speed conditioning, mental toughness training, and full 11v11 match preparation. Players receive individual performance reports and video breakdowns to track their development throughout the season.',
            'age_min': 13, 'age_max': 14, 'max_capacity': 14,
            'program_type': 'group',
            'price_per_session': 5500, 'price_monthly': 19900,
            'sibling_discount_pct': 10,
            'color': '#F44336',
        },
        {
            'name': '1-on-1 Private Training',
            'slug': 'one-on-one',
            'description': 'Fully personalized private sessions with Coach Alex Rivera, tailored to your child\'s specific development goals. Whether preparing for tryouts, recovering confidence after an injury, or accelerating technical growth, every minute is devoted to your player. Includes a written progress report after every four sessions and a customized at-home practice plan.',
            'age_min': 4, 'age_max': 16, 'max_capacity': 1,
            'program_type': 'private',
            'price_per_session': 8500, 'price_monthly': 29900,
            'sibling_discount_pct': 0,
            'color': '#9C27B0',
        },
    ]

    for pd in programs_data:
        db.session.add(Program(**pd))
    db.session.flush()

    # Generate sample sessions for the next 4 weeks
    programs = Program.query.all()
    base_date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    days_until_monday = (7 - base_date.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_monday = base_date + timedelta(days=days_until_monday)

    schedule = {
        'little-kicks': [('Tuesday', 16, 0), ('Saturday', 9, 0)],
        'junior-stars': [('Monday', 16, 30), ('Wednesday', 16, 30), ('Saturday', 10, 0)],
        'skill-builders': [('Tuesday', 17, 0), ('Thursday', 17, 0), ('Saturday', 10, 30)],
        'rising-players': [('Monday', 17, 30), ('Thursday', 17, 30), ('Saturday', 11, 30)],
        'elite-youth': [('Tuesday', 18, 0), ('Friday', 17, 30), ('Saturday', 12, 30)],
        'one-on-one': [('Monday', 15, 0), ('Wednesday', 15, 0), ('Friday', 15, 0)],
    }

    day_offsets = {'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
                   'Friday': 4, 'Saturday': 5, 'Sunday': 6}

    for program in programs:
        if program.slug in schedule:
            for day_name, hour, minute in schedule[program.slug]:
                for week in range(4):
                    session_date = next_monday + timedelta(days=day_offsets[day_name] + week * 7)
                    start = session_date.replace(hour=hour, minute=minute)
                    duration = 60 if program.slug != 'one-on-one' else 45
                    end = start + timedelta(minutes=duration)
                    sess = ClassSession(
                        program_id=program.id,
                        title=f'{program.name}',
                        start_time=start,
                        end_time=end,
                        location='Main Training Field',
                        season='spring'
                    )
                    db.session.add(sess)
    db.session.commit()

# ─── Init ─────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    seed_database()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
