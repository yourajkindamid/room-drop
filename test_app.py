from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_socketio import SocketIO, join_room as sio_join_room, leave_room as sio_leave_room, emit
import psycopg2
import bcrypt
import boto3
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
import os
import uuid
import secrets
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

ROOM_LIFETIME_MINUTES = 45
CLEANUP_INTERVAL_SECONDS = 300
R2_BATCH_DELETE_SIZE = 1000

USER_ROOM_CAP_BYTES = 500 * 1024 * 1024
GUEST_ROOM_CAP_BYTES = 200 * 1024 * 1024


def get_db_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def get_r2_client():
    account_id = os.getenv('R2_ACCOUNT_ID')
    if not account_id:
        raise RuntimeError("R2_ACCOUNT_ID not set in environment")
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        service_name='s3',
        endpoint_url=endpoint_url,
        aws_access_key_id=os.getenv('R2_ACCESS_KEY'),
        aws_secret_access_key=os.getenv('R2_SECRET_KEY'),
        region_name='auto',
    )


def validate_r2_setup():
    required = ['R2_ACCESS_KEY', 'R2_SECRET_KEY', 'R2_ACCOUNT_ID', 'R2_BUCKET_NAME']
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"R2 env vars missing: {', '.join(missing)}")

    bucket = os.getenv('R2_BUCKET_NAME')
    try:
        client = get_r2_client()
        client.head_bucket(Bucket=bucket)
        client.list_objects_v2(Bucket=bucket, MaxKeys=1)
        app.logger.info(f"R2 setup OK: bucket={bucket}")
    except NoCredentialsError:
        raise RuntimeError("R2 credentials not loaded by boto3 — check .env")
    except EndpointConnectionError as e:
        raise RuntimeError(f"Cannot reach R2 endpoint. Check R2_ACCOUNT_ID. Detail: {e}")
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', 'Unknown')
        if code == 'InvalidAccessKeyId':
            raise RuntimeError("R2 access key is wrong (InvalidAccessKeyId).")
        if code == 'SignatureDoesNotMatch':
            raise RuntimeError("R2 secret key is wrong or truncated.")
        if code == 'NoSuchBucket':
            raise RuntimeError(
                f"R2 bucket '{bucket}' does not exist or token is scoped elsewhere."
            )
        if code in ('AccessDenied', '403', 'Forbidden'):
            raise RuntimeError(
                f"R2 access denied for bucket '{bucket}'. "
                f"Check token has Object Read & Write and is scoped to this bucket."
            )
        raise RuntimeError(f"R2 setup check failed ({code}): {e}")


def insert_log(conn, event_type, room_id=None, user_id=None, guest_id=None, ip_address=None, message=None):
    try:
        cur = conn.cursor()
        cur.execute(
            'insert into logs (event_type, room_id, user_id, guest_id, ip_address, message) values (%s, %s, %s, %s, %s, %s)',
            (event_type, room_id, user_id, guest_id, ip_address, message),
        )
        cur.close()
    except Exception as e:
        app.logger.error(f"insert_log failed: event_type={event_type} error={e}")
        raise


def get_or_create_guest(conn, room_id):
    try:
        tokens = session.get('guest_tokens')
        if not isinstance(tokens, dict):
            tokens = {}

        token = tokens.get(room_id)
        cur = conn.cursor()

        if token:
            cur.execute(
                'select id from guests where guest_token = %s and room_id = %s',
                (token, room_id),
            )
            row = cur.fetchone()
            if row:
                cur.close()
                return row[0]

        new_token = str(uuid.uuid4())
        cur.execute(
            'insert into guests (guest_token, room_id) values (%s, %s) returning id',
            (new_token, room_id),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()

        tokens[room_id] = new_token
        session['guest_tokens'] = tokens
        session.modified = True
        return new_id
    except Exception as e:
        app.logger.error(f"get_or_create_guest failed: room_id={room_id} error={e}")
        raise


def _guest_display_name(guest_token):
    if not guest_token:
        return 'Guest'
    prefix = guest_token.replace('-', '')[:4].upper()  # first 4 chars give a nice short tag
    return f"Guest-{prefix}"


def _resolve_actor_name(conn, user_id, guest_id):
    if user_id:
        cur = conn.cursor()
        cur.execute('select username from users where id = %s', (user_id,))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else 'User'
    if guest_id:
        cur = conn.cursor()
        cur.execute('select guest_token from guests where id = %s', (guest_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            return _guest_display_name(row[0])
    return 'Guest'


def _resolve_room(conn, room_id):
    cur = conn.cursor()
    cur.execute('select 1 from rooms where room_id = %s', (room_id,))
    if cur.fetchone():
        cur.close()
        return 'user'
    cur.execute('select 1 from guest_room where room_id = %s', (room_id,))
    found = cur.fetchone() is not None
    cur.close()
    return 'guest' if found else None


def _generate_room_id(conn, length=8, max_attempts=10):
    alphabet = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'
    cur = conn.cursor()
    for _ in range(max_attempts):
        candidate = ''.join(secrets.choice(alphabet) for _ in range(length))
        cur.execute('select 1 from rooms where room_id = %s', (candidate,))
        if cur.fetchone():
            continue
        cur.execute('select 1 from guest_room where room_id = %s', (candidate,))
        if cur.fetchone():
            continue
        cur.close()
        return candidate
    cur.close()
    raise RuntimeError("Could not generate unique room_id after max attempts")


def _humanize_remaining(expires_at):
    if expires_at is None:
        return "—"
    now = datetime.utcnow()
    if expires_at.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=None)
    delta = expires_at - now
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes <= 0:
        return "expired"
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _humanize_bytes(n):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != 'B' else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _room_used_bytes(conn, room_id):
    cur = conn.cursor()
    cur.execute(
        'select coalesce(sum(file_size), 0) from files where room_id = %s',
        (room_id,),
    )
    used = cur.fetchone()[0]
    cur.close()
    return int(used or 0)


def _room_cap_bytes(room_type):
    return USER_ROOM_CAP_BYTES if room_type == 'user' else GUEST_ROOM_CAP_BYTES


def _room_members(conn, room_id):
    cur = conn.cursor()
    cur.execute(
        'select distinct m.user_id, m.guest_id, u.username, g.guest_token '
        'from room_members m '
        'left join users u on u.id = m.user_id '
        'left join guests g on g.id = m.guest_id '
        'where m.room_id = %s and m.left_at is null',
        (room_id,),
    )
    rows = cur.fetchall()
    cur.close()

    seen = set()
    out = []
    for user_id, guest_id, username, guest_token in rows:
        if user_id is not None:
            key = f"u-{user_id}"
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": username or "User", "kind": "user", "id_hash": key})
        elif guest_id is not None:
            key = f"g-{(guest_token or '').replace('-', '')[:8]}"
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "name": _guest_display_name(guest_token),
                "kind": "guest",
                "id_hash": key,
            })
    return out


def _broadcast_members(room_id):
    conn = get_db_connection()
    try:
        members = _room_members(conn, room_id)
    finally:
        conn.close()
    socketio.emit('members_changed', {'room_id': room_id, 'members': members}, to=room_id)


def _r2_delete_keys_batch(r2, bucket, keys):
    if not keys:
        return 0, []
    success = 0
    failed = []
    for i in range(0, len(keys), R2_BATCH_DELETE_SIZE):
        chunk = keys[i:i + R2_BATCH_DELETE_SIZE]
        try:
            resp = r2.delete_objects(
                Bucket=bucket,
                Delete={'Objects': [{'Key': k} for k in chunk], 'Quiet': False},
            )
            success += len(resp.get('Deleted', []))
            for err in resp.get('Errors', []):
                failed.append((err.get('Key'), err.get('Code'), err.get('Message')))
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', 'Unknown')
            app.logger.error(f"R2 batch delete failed: code={code} error={e}")
            for k in chunk:
                failed.append((k, code, str(e)))
        except Exception as e:
            app.logger.error(f"R2 batch delete unexpected error: {e}")
            for k in chunk:
                failed.append((k, 'Unknown', str(e)))
    return success, failed


def _r2_list_prefix(r2, bucket, prefix):
    keys = []
    continuation = None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': prefix}
        if continuation:
            kwargs['ContinuationToken'] = continuation
        try:
            resp = r2.list_objects_v2(**kwargs)
        except ClientError as e:
            app.logger.error(f"R2 list_objects_v2 failed prefix={prefix}: {e}")
            return keys
        for obj in resp.get('Contents', []):
            keys.append(obj['Key'])
        if resp.get('IsTruncated'):
            continuation = resp.get('NextContinuationToken')
        else:
            break
    return keys


def _purge_room_storage(r2, bucket, room_id):
    if not room_id or '/' in room_id:
        raise ValueError(f"Refusing to purge storage for invalid room_id: {room_id!r}")
    prefix = f"{room_id}/"
    keys = _r2_list_prefix(r2, bucket, prefix)
    if not keys:
        return 0, []
    return _r2_delete_keys_batch(r2, bucket, keys)


@app.route('/', methods=['GET'])
def home_page():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return render_template('landing.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                'insert into users (username, email, password_hash) values (%s, %s, %s)',
                (username, email, password_hash),
            )
            conn.commit()
            flash('Account created successfully! Please log in.', 'success')
            return redirect(url_for('login'))
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            flash('Username or email already exists.', 'error')
            return redirect(url_for('signup'))
        finally:
            cur.close()
            conn.close()
    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '')

        if not identifier or not password:
            flash('Please provide both username/email and password.', 'error')
            return redirect(url_for('login'))

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                'select id, username, password_hash from users where username = %s or email = %s',
                (identifier, identifier),
            )
            row = cur.fetchone()

            if row is None:
                try:
                    insert_log(conn, event_type='login_fail',
                               ip_address=request.remote_addr,
                               message=f"unknown user: {identifier}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                flash('Invalid credentials.', 'error')
                return redirect(url_for('login'))

            user_id, username, stored_hash = row
            if bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
                session['user_id'] = user_id
                session['username'] = username
                try:
                    insert_log(conn, event_type='login_success', user_id=user_id,
                               ip_address=request.remote_addr)
                    conn.commit()
                except Exception:
                    conn.rollback()
                return redirect(url_for('dashboard'))
            else:
                try:
                    insert_log(conn, event_type='login_fail', user_id=user_id,
                               ip_address=request.remote_addr, message='bad password')
                    conn.commit()
                except Exception:
                    conn.rollback()
                flash('Invalid credentials.', 'error')
                return redirect(url_for('login'))
        except psycopg2.Error as e:
            app.logger.error(f"login DB error: {e}")
            conn.rollback()
            flash('Something went wrong. Please try again.', 'error')
            return redirect(url_for('login'))
        finally:
            cur.close()
            conn.close()
    return render_template('login.html')


@app.route('/logout', methods=['GET'])
def logout():
    session.clear()
    return redirect(url_for('home_page'))


@app.route('/dashboard', methods=['GET'])
def dashboard():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'select r.room_id, r.room_name, r.expires_at, '
            '(select count(*) from files f where f.room_id = r.room_id), '
            '(select count(*) from room_members m where m.room_id = r.room_id and m.left_at is null), '
            '(select coalesce(sum(file_size), 0) from files f where f.room_id = r.room_id) '
            'from rooms r '
            'where r.owner_user_id = %s and r.is_active = true '
            'order by r.created_at desc',
            (user_id,),
        )
        rows = cur.fetchall()

        rooms = [
            {
                "room_id": r[0],
                "room_name": r[1],
                "expires_in": _humanize_remaining(r[2]),
                "file_count": r[3],
                "member_count": r[4],
                "used_human": _humanize_bytes(int(r[5] or 0)),
                "cap_human": _humanize_bytes(USER_ROOM_CAP_BYTES),
            }
            for r in rows
        ]
        return render_template('dashboard.html', rooms=rooms, username=session.get('username', ''))
    finally:
        cur.close()
        conn.close()


@app.route('/create-room', methods=['GET', 'POST'])
def create_room():
    user_id = session.get('user_id')
    if not user_id:
        flash('Log in to create a persistent room.', 'error')
        return redirect(url_for('login'))

    if request.method == 'POST':
        room_name = (request.form.get('room_name') or '').strip()
        if not room_name:
            flash('Room name is required.', 'error')
            return redirect(url_for('create_room'))

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            new_room_id = _generate_room_id(conn)
            expires_at = datetime.utcnow() + timedelta(minutes=ROOM_LIFETIME_MINUTES)
            cur.execute(
                'insert into rooms (room_id, room_name, owner_user_id, room_type, expires_at, is_active) values (%s, %s, %s, \'user\', %s, true)',
                (new_room_id, room_name, user_id, expires_at),
            )
            insert_log(conn, event_type='room_create', room_id=new_room_id, user_id=user_id,
                       ip_address=request.remote_addr, message=room_name)
            conn.commit()
            return redirect(url_for('view_room', room_id=new_room_id))
        except psycopg2.Error as e:
            app.logger.error(f"create_room DB error: {e}")
            conn.rollback()
            flash('Could not create room. Please try again.', 'error')
            return redirect(url_for('create_room'))
        finally:
            cur.close()
            conn.close()
    return render_template('create_room.html')


@app.route('/create-guest-room', methods=['GET', 'POST'])
def create_guest_room():
    if request.method == 'POST':
        room_name = (request.form.get('room_name') or '').strip()
        if not room_name:
            flash('Room name is required.', 'error')
            return redirect(url_for('create_guest_room'))

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            new_room_id = _generate_room_id(conn)
            expires_at = datetime.utcnow() + timedelta(minutes=ROOM_LIFETIME_MINUTES)

            owner_token = str(uuid.uuid4())
            cur.execute(
                'insert into guests (guest_token, room_id) values (%s, %s) returning id',
                (owner_token, new_room_id),
            )
            owner_guest_id = cur.fetchone()[0]

            cur.execute(
                'insert into guest_room (room_id, room_name, guest_owner_id, expires_at, is_active) values (%s, %s, %s, %s, true)',
                (new_room_id, room_name, owner_guest_id, expires_at),
            )
            conn.commit()

            tokens = session.get('guest_tokens')
            if not isinstance(tokens, dict):
                tokens = {}
            tokens[new_room_id] = owner_token
            session['guest_tokens'] = tokens
            session.modified = True

            return redirect(url_for('view_guest_room', room_id=new_room_id))
        except psycopg2.Error as e:
            app.logger.error(f"create_guest_room DB error: {e}")
            conn.rollback()
            flash('Could not create room. Please try again.', 'error')
            return redirect(url_for('create_guest_room'))
        finally:
            cur.close()
            conn.close()
    return render_template('create_guest_room.html')


@app.route('/delete-room/<room_id>', methods=['POST'])
def delete_room(room_id):
    if not room_id or not room_id.strip():
        flash('Invalid room ID.', 'error')
        return redirect(url_for('home_page'))
    room_id = room_id.strip()

    user_id = session.get('user_id')
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('select owner_user_id from rooms where room_id = %s', (room_id,))
        urow = cur.fetchone()
        cur.execute('select guest_owner_id from guest_room where room_id = %s', (room_id,))
        grow = cur.fetchone()

        if urow is None and grow is None:
            flash('Room not found.', 'error')
            return redirect(url_for('home_page'))

        is_user_room = urow is not None
        if is_user_room:
            if not user_id or urow[0] != user_id:
                flash('You do not have permission to delete this room.', 'error')
                return redirect(url_for('view_room', room_id=room_id))
        else:
            tokens = session.get('guest_tokens') or {}
            owner_token = tokens.get(room_id)
            if not owner_token:
                flash('You do not have permission to delete this room.', 'error')
                return redirect(url_for('view_guest_room', room_id=room_id))
            cur.execute(
                'select id from guests where guest_token = %s and room_id = %s',
                (owner_token, room_id),
            )
            grow_check = cur.fetchone()
            if grow_check is None or grow_check[0] != grow[0]:
                flash('You do not have permission to delete this room.', 'error')
                return redirect(url_for('view_guest_room', room_id=room_id))

        success = _cleanup_single_room(conn, room_id, room_type=('user' if is_user_room else 'guest'))
        if not success:
            flash('Cleanup failed. Try again or wait for automatic expiry.', 'error')
            return redirect(url_for('view_room' if is_user_room else 'view_guest_room', room_id=room_id))

        socketio.emit('room_deleted', {'room_id': room_id}, to=room_id)  # let clients know it's gone

        flash(f'Room {room_id} deleted.', 'success')
        return redirect(url_for('dashboard') if user_id else url_for('home_page'))
    finally:
        cur.close()
        conn.close()


@app.route('/join-room', methods=['GET', 'POST'])
def join_room_form():
    if request.method == 'POST':
        raw_id = (request.form.get('room_id') or '').strip().upper()
        if not raw_id:
            flash('Please enter a Room ID.', 'error')
            return redirect(url_for('join_room_form'))

        conn = get_db_connection()
        try:
            room_type = _resolve_room(conn, raw_id)
            if room_type is None:
                flash(f'Room "{raw_id}" not found. Check the ID and try again.', 'error')
                return redirect(url_for('join_room_form'))
            if room_type == 'user':
                return redirect(url_for('view_room', room_id=raw_id))
            return redirect(url_for('view_guest_room', room_id=raw_id))
        finally:
            conn.close()

    return render_template('join_room.html', logged_in=bool(session.get('user_id')))


def _is_room_owner(conn, room_id, room_type, user_id, guest_tokens):
    cur = conn.cursor()
    if room_type == 'user':
        cur.execute('select owner_user_id from rooms where room_id = %s', (room_id,))
        row = cur.fetchone()
        cur.close()
        return bool(user_id and row and row[0] == user_id)
    else:
        cur.execute('select guest_owner_id from guest_room where room_id = %s', (room_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return False
        owner_id = row[0]
        owner_token = (guest_tokens or {}).get(room_id)
        if not owner_token:
            cur.close()
            return False
        cur.execute(
            'select id from guests where guest_token = %s and room_id = %s',
            (owner_token, room_id),
        )
        check = cur.fetchone()
        cur.close()
        return bool(check and check[0] == owner_id)


@app.route('/room/<room_id>', methods=['GET'])
def view_room(room_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'select room_id, room_name, expires_at, is_active from rooms where room_id = %s',
            (room_id,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute('select 1 from guest_room where room_id = %s', (room_id,))
            if cur.fetchone():
                return redirect(url_for('view_guest_room', room_id=room_id))
            flash('Room not found.', 'error')
            return redirect(url_for('home_page'))
        if not row[3]:
            flash('This room has expired.', 'error')
            return redirect(url_for('home_page'))

        is_owner = _is_room_owner(
            conn, room_id, 'user',
            session.get('user_id'),
            session.get('guest_tokens') or {},
        )

        room = {
            "room_id": row[0],
            "room_name": row[1],
            "expires_in": _humanize_remaining(row[2]),
            "cap_bytes": USER_ROOM_CAP_BYTES,
            "cap_human": _humanize_bytes(USER_ROOM_CAP_BYTES),
        }
        return render_template(
            'room.html',
            room=room,
            username=session.get('username') or 'Guest',
            logged_in=bool(session.get('user_id')),
            is_owner=is_owner,
        )
    finally:
        cur.close()
        conn.close()


@app.route('/guest-room/<room_id>', methods=['GET'])
def view_guest_room(room_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'select room_id, room_name, expires_at, is_active from guest_room where room_id = %s',
            (room_id,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute('select 1 from rooms where room_id = %s', (room_id,))
            if cur.fetchone():
                return redirect(url_for('view_room', room_id=room_id))
            flash('Room not found.', 'error')
            return redirect(url_for('home_page'))
        if not row[3]:
            flash('This room has expired.', 'error')
            return redirect(url_for('home_page'))

        is_owner = _is_room_owner(
            conn, room_id, 'guest',
            session.get('user_id'),
            session.get('guest_tokens') or {},
        )

        room = {
            "room_id": row[0],
            "room_name": row[1],
            "expires_in": _humanize_remaining(row[2]),
            "cap_bytes": GUEST_ROOM_CAP_BYTES,
            "cap_human": _humanize_bytes(GUEST_ROOM_CAP_BYTES),
        }
        return render_template('guest_room.html', room=room, is_owner=is_owner)
    finally:
        cur.close()
        conn.close()


@app.route('/members/<room_id>', methods=['GET'])
def list_members(room_id):
    if not room_id or not room_id.strip():
        return jsonify({"error": "room_id is required"}), 400
    room_id = room_id.strip()

    conn = get_db_connection()
    try:
        if _resolve_room(conn, room_id) is None:
            return jsonify({"error": "Room not found"}), 404
        return jsonify(_room_members(conn, room_id)), 200
    finally:
        conn.close()


@app.route('/upload', methods=['POST'])
def upload_file():
    file = request.files.get('file')
    if file is None or not file.filename or file.filename == '':
        return jsonify({"error": "No file provided"}), 400

    raw_room_id = request.form.get('room_id')
    if not raw_room_id or not raw_room_id.strip():
        return jsonify({"error": "room_id is required"}), 400
    room_id = raw_room_id.strip()

    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        return jsonify({"error": "Empty file"}), 400

    uuid_no_hyphens = uuid.uuid4().hex
    original_filename = file.filename
    sanitized_filename = original_filename.strip().replace(' ', '_')
    file_key = f"{room_id}/{uuid_no_hyphens}_{sanitized_filename}"

    account_id = os.getenv('R2_ACCOUNT_ID')
    bucket_name = os.getenv('R2_BUCKET_NAME')
    filepath = f"https://{account_id}.r2.cloudflarestorage.com/{bucket_name}/{file_key}"

    conn = get_db_connection()
    cur = None
    try:
        room_type = _resolve_room(conn, room_id)
        if room_type is None:
            return jsonify({"error": "Room not found"}), 404

        cap = _room_cap_bytes(room_type)
        used = _room_used_bytes(conn, room_id)
        if size > cap:
            return jsonify({
                "error": "File too large for this room",
                "cap": cap,
                "cap_human": _humanize_bytes(cap),
            }), 413
        if used + size > cap:
            remaining = max(cap - used, 0)
            return jsonify({
                "error": "Room storage limit reached",
                "used": used, "used_human": _humanize_bytes(used),
                "cap": cap, "cap_human": _humanize_bytes(cap),
                "remaining": remaining, "remaining_human": _humanize_bytes(remaining),
            }), 413

        user_id = session.get('user_id')
        guest_id = None
        if not user_id:
            guest_id = get_or_create_guest(conn, room_id)

        try:
            r2 = get_r2_client()
            r2.upload_fileobj(Fileobj=file.stream, Bucket=bucket_name, Key=file_key)
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', 'Unknown')
            app.logger.error(
                f"R2 upload ClientError: code={code} room_id={room_id} "
                f"key={file_key} bucket={bucket_name} error={e}"
            )
            if room_type == 'user':
                try:
                    insert_log(conn, event_type='file_upload_failed',
                               room_id=room_id, user_id=user_id, guest_id=guest_id,
                               ip_address=request.remote_addr, message=f"R2 {code}")
                    conn.commit()
                except Exception as log_err:
                    app.logger.error(f"Could not log file_upload_failed: {log_err}")
                    conn.rollback()
            if code == 'AccessDenied':
                return jsonify({
                    "error": "Upload to storage failed",
                    "hint": "R2 token lacks write permission. Check Cloudflare API token.",
                }), 500
            return jsonify({"error": "Upload to storage failed"}), 500
        except Exception as e:
            app.logger.error(f"R2 upload unexpected error: room_id={room_id} key={file_key} error={e}")
            if room_type == 'user':
                try:
                    insert_log(conn, event_type='file_upload_failed',
                               room_id=room_id, user_id=user_id, guest_id=guest_id,
                               ip_address=request.remote_addr,
                               message='R2 upload failed (unexpected)')
                    conn.commit()
                except Exception:
                    conn.rollback()
            return jsonify({"error": "Upload to storage failed"}), 500

        try:
            cur = conn.cursor()
            cur.execute(
                'insert into files (room_id, file_name, file_key, filepath, file_size, user_id, guest_id) values (%s, %s, %s, %s, %s, %s, %s)',
                (room_id, original_filename, file_key, filepath, size, user_id, guest_id),
            )
            if room_type == 'user':
                insert_log(conn, event_type='file_upload',
                           room_id=room_id, user_id=user_id, guest_id=guest_id,
                           ip_address=request.remote_addr, message=original_filename)
            conn.commit()
        except psycopg2.Error as e:
            app.logger.error(f"DB insert for file record failed: room_id={room_id} error={e}")
            conn.rollback()
            try:
                r2 = get_r2_client()
                r2.delete_object(Bucket=bucket_name, Key=file_key)
                app.logger.info(f"Cleaned up orphaned R2 object after DB failure: {file_key}")
            except Exception as cleanup_err:
                app.logger.error(f"Could not clean up orphan: {file_key} error={cleanup_err}")
            return jsonify({"error": "Database error during file record creation"}), 500

        socketio.emit('new_file', {'room_id': room_id}, to=room_id)  # tell others a file just landed

        return jsonify({
            "message": "upload successful",
            "file_key": file_key,
            "size": size,
        }), 200
    finally:
        if cur is not None:
            try: cur.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass


@app.route('/files/<room_id>', methods=['GET'])
def list_files(room_id):
    if not room_id or not room_id.strip():
        return jsonify({"error": "room_id is required"}), 400
    room_id = room_id.strip()

    conn = get_db_connection()
    cur = None
    try:
        try:
            cur = conn.cursor()
            cur.execute(
                'select file_name, file_key, file_size from files where room_id = %s order by uploaded_at asc',
                (room_id,),
            )
            rows = cur.fetchall()
        except psycopg2.Error as e:
            app.logger.error(f"DB select for /files/{room_id} failed: {e}")
            conn.rollback()
            return jsonify({"error": "Database error"}), 500

        bucket_name = os.getenv('R2_BUCKET_NAME')
        r2 = get_r2_client()
        result = []
        for file_name, file_key, file_size in rows:
            try:
                url = r2.generate_presigned_url(
                    ClientMethod='get_object',
                    Params={'Bucket': bucket_name, 'Key': file_key},
                    ExpiresIn=3600,  # urls die after an hour, that's fine
                )
            except Exception as e:
                app.logger.error(f"Presign failed: room_id={room_id} key={file_key} error={e}")
                continue
            result.append({
                "file_name": file_name,
                "url": url,
                "size": int(file_size or 0),
                "size_human": _humanize_bytes(int(file_size or 0)),
            })
        return jsonify(result), 200
    finally:
        if cur is not None:
            try: cur.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass


@socketio.on('join_room')
def handle_join_room(data):
    room_id = (data or {}).get('room_id')
    if not room_id or not str(room_id).strip():
        emit('error', {'message': 'room_id is required'})
        return
    room_id = str(room_id).strip()

    sio_join_room(room_id)

    conn = get_db_connection()
    cur = None
    try:
        try:
            room_type = _resolve_room(conn, room_id)
            if room_type is None:
                emit('error', {'message': 'Room not found'})
                return

            user_id = session.get('user_id')
            guest_id = None
            if not user_id:
                guest_id = get_or_create_guest(conn, room_id)

            cur = conn.cursor()
            cur.execute(
                'insert into room_members (room_id, user_id, guest_id) values (%s, %s, %s)',
                (room_id, user_id, guest_id),
            )
            if room_type == 'user':
                insert_log(conn, event_type='room_join',
                           room_id=room_id, user_id=user_id, guest_id=guest_id,
                           ip_address=request.remote_addr if request else None)
            conn.commit()

            display_name = _resolve_actor_name(conn, user_id, guest_id)
        except psycopg2.Error as e:
            app.logger.error(f"join_room DB failure: room_id={room_id} error={e}")
            conn.rollback()
            emit('error', {'message': 'Could not join room'})
            return
        except Exception as e:
            app.logger.error(f"join_room unexpected failure: room_id={room_id} error={e}")
            conn.rollback()
            emit('error', {'message': 'Could not join room'})
            return
    finally:
        if cur is not None:
            try: cur.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass

    emit('room_joined', {'room_id': room_id, 'your_name': display_name})
    _broadcast_members(room_id)


@socketio.on('list_members')
def handle_list_members(data):
    room_id = (data or {}).get('room_id')
    if not room_id or not str(room_id).strip():
        return
    room_id = str(room_id).strip()
    conn = get_db_connection()
    try:
        members = _room_members(conn, room_id)
    finally:
        conn.close()
    emit('members_changed', {'room_id': room_id, 'members': members})


@socketio.on('send_message')
def handle_send_message(data):
    data = data or {}
    room_id = data.get('room_id')
    content = data.get('content')

    if not room_id or not str(room_id).strip():
        emit('error', {'message': 'room_id is required'})
        return
    room_id = str(room_id).strip()

    if content is None or not str(content).strip():
        emit('error', {'message': 'content is required'})
        return
    if len(content) > 10000:
        emit('error', {'message': 'Message too long'})
        return

    m_length = len(content)

    conn = get_db_connection()
    cur = None
    display_name = 'Guest'
    try:
        try:
            room_type = _resolve_room(conn, room_id)
            if room_type is None:
                emit('error', {'message': 'Room not found'})
                return
            if room_type == 'guest':
                emit('error', {'message': 'Messaging is not allowed in this room'})
                return

            user_id = session.get('user_id')
            guest_id = None
            if not user_id:
                guest_id = get_or_create_guest(conn, room_id)

            display_name = _resolve_actor_name(conn, user_id, guest_id)  # server side, client can't fake this

            cur = conn.cursor()
            cur.execute(
                'insert into messages (room_id, user_id, guest_id, m_length) values (%s, %s, %s, %s)',
                (room_id, user_id, guest_id, m_length),
            )
            insert_log(conn, event_type='message_sent',
                       room_id=room_id, user_id=user_id, guest_id=guest_id,
                       ip_address=request.remote_addr if request else None)
            conn.commit()
        except psycopg2.Error as e:
            app.logger.error(f"send_message DB failure: room_id={room_id} error={e}")
            conn.rollback()
            emit('error', {'message': 'Message could not be recorded'})
            return
        except Exception as e:
            app.logger.error(f"send_message unexpected failure: room_id={room_id} error={e}")
            conn.rollback()
            emit('error', {'message': 'Message could not be recorded'})
            return
    finally:
        if cur is not None:
            try: cur.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass

    emit(
        'new_message',
        {
            'room_id': room_id,
            'username': display_name,
            'content': content,
            'sent_at': datetime.utcnow().isoformat(),
        },
        to=room_id,
    )


@socketio.on('leave_room')
def handle_leave_room(data):
    room_id = (data or {}).get('room_id')
    if not room_id or not str(room_id).strip():
        emit('error', {'message': 'room_id is required'})
        return
    room_id = str(room_id).strip()

    sio_leave_room(room_id)

    conn = get_db_connection()
    cur = None
    try:
        try:
            room_type = _resolve_room(conn, room_id)
            if room_type is None:
                emit('error', {'message': 'Room not found'})
                return

            user_id = session.get('user_id')
            guest_id = None
            if not user_id:
                guest_id = get_or_create_guest(conn, room_id)

            cur = conn.cursor()
            cur.execute(
                'update room_members set left_at = now() '
                'where room_id = %s and user_id is not distinct from %s '
                'and guest_id is not distinct from %s and left_at is null',
                (room_id, user_id, guest_id),
            )
            if room_type == 'user':
                insert_log(conn, event_type='room_leave',
                           room_id=room_id, user_id=user_id, guest_id=guest_id,
                           ip_address=request.remote_addr if request else None)
            conn.commit()
        except psycopg2.Error as e:
            app.logger.error(f"leave_room DB failure: room_id={room_id} error={e}")
            conn.rollback()
            emit('error', {'message': 'Could not update leave status'})
            return
        except Exception as e:
            app.logger.error(f"leave_room unexpected failure: room_id={room_id} error={e}")
            conn.rollback()
            emit('error', {'message': 'Could not update leave status'})
            return
    finally:
        if cur is not None:
            try: cur.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass

    emit('room_left', {'room_id': room_id}, to=room_id, include_self=False)
    _broadcast_members(room_id)


def _cleanup_single_room(conn, room_id, room_type):
    if room_type not in ('user', 'guest'):
        app.logger.error(f"_cleanup_single_room: invalid room_type={room_type}")
        return False

    bucket_name = os.getenv('R2_BUCKET_NAME')
    try:
        r2 = get_r2_client()
        purged, failed = _purge_room_storage(r2, bucket_name, room_id)
    except Exception as e:
        app.logger.error(f"R2 purge failed for room_id={room_id}: {e}")
        return False

    if failed:
        app.logger.error(
            f"R2 purge for room_id={room_id} had {len(failed)} failures "
            f"(of {purged + len(failed)} total). Skipping room."
        )
        for k, code, msg in failed[:5]:
            app.logger.error(f"  - key={k} code={code} msg={msg}")
        return False

    app.logger.info(f"R2 purge OK for room_id={room_id}: {purged} object(s) deleted")

    cur = conn.cursor()
    try:
        cur.execute('delete from files where room_id = %s', (room_id,))
        cur.execute('delete from room_members where room_id = %s', (room_id,))
        if room_type == 'user':
            cur.execute('delete from messages where room_id = %s', (room_id,))
            cur.execute('delete from guests where room_id = %s', (room_id,))
            cur.execute('update rooms set is_active = false where room_id = %s', (room_id,))
            insert_log(conn, event_type='room_expired', room_id=room_id, message='Room cleaned up')
        else:
            cur.execute('update guest_room set guest_owner_id = null, is_active = false where room_id = %s', (room_id,))
            cur.execute('delete from guests where room_id = %s', (room_id,))
        conn.commit()
        app.logger.info(f"Cleaned up {room_type}-room: {room_id}")
        return True
    except psycopg2.Error as e:
        app.logger.error(f"DB cleanup failed for {room_type}-room {room_id}: {e}")
        conn.rollback()
        return False
    finally:
        try: cur.close()
        except Exception: pass


def cleanup_expired_rooms():
    conn = None
    cleaned_count = 0
    failed_count = 0
    try:
        conn = get_db_connection()

        cur = conn.cursor()
        cur.execute('select room_id from rooms where expires_at < now() and is_active = true')
        expired_user = [row[0] for row in cur.fetchall()]
        cur.close()

        for room_id in expired_user:
            ok = _cleanup_single_room(conn, room_id, room_type='user')
            cleaned_count += int(ok)
            failed_count += int(not ok)

        cur = conn.cursor()
        cur.execute('select room_id from guest_room where expires_at < now() and is_active = true')
        expired_guest = [row[0] for row in cur.fetchall()]
        cur.close()

        for room_id in expired_guest:
            ok = _cleanup_single_room(conn, room_id, room_type='guest')
            cleaned_count += int(ok)
            failed_count += int(not ok)

        total = len(expired_user) + len(expired_guest)
        if total == 0:
            app.logger.info("cleanup_expired_rooms: no expired rooms")
        else:
            app.logger.info(
                f"cleanup_expired_rooms: {cleaned_count} cleaned, "
                f"{failed_count} failed, {total} total"
            )
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


_scheduler_thread = None
_scheduler_stop = threading.Event()


def _scheduler_loop():
    app.logger.info(f"Cleanup scheduler started (interval={CLEANUP_INTERVAL_SECONDS}s)")
    if _scheduler_stop.wait(timeout=10):
        return
    while not _scheduler_stop.is_set():
        try:
            with app.app_context():
                cleanup_expired_rooms()
        except Exception as e:
            app.logger.error(f"Scheduler iteration failed: {e}")
        if _scheduler_stop.wait(timeout=CLEANUP_INTERVAL_SECONDS):
            break
    app.logger.info("Cleanup scheduler stopped")


def start_scheduler():
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)  # daemon so it doesn't block shutdown
    _scheduler_thread.start()


if __name__ == '__main__':
    try:
        validate_r2_setup()
    except RuntimeError as e:
        app.logger.error(f"FATAL: R2 setup invalid — {e}")
        raise SystemExit(1)

    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_scheduler()

    socketio.run(app, host='0.0.0.0')
