from flask import Flask, render_template, request, redirect, url_for, flash,session,abort
import psycopg2
import bcrypt
import string
import secrets

# db connection string - keep this out of version control ideally
DATABASE_URL = "postgresql://postgres.fzuoyaxlwwsanlsicmoo:hosh_me_aao_abhijeet_69@aws-1-ap-south-1.pooler.supabase.com:5432/postgres"
app = Flask(__name__)
# secret key for session signing, change this in prod
app.secret_key = 'diddy_blud_managment_system'

# sets up a cloudflare r2 client (s3-compatible storage)
def get_r2_client():
    account_id = os.getenv('R2_ACCOUNT_ID')
    endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        service_name='s3',
        endpoint_url=endpoint_url,
        aws_access_key_id=os.getenv('R2_ACCESS_KEY'),
        aws_secret_access_key=os.getenv('R2_SECRET_KEY'),
        region_name='auto',
    )

@app.route('/upload', methods=['POST'])
def upload_file():
    file = request.files.get('file')
    # basic checks before doing anything
    if file is None or not file.filename or file.filename == '':
        return jsonify({"error": "No file provided"}), 400

    raw_room_id = request.form.get('room_id')
    if not raw_room_id or not raw_room_id.strip():
        return jsonify({"error": "room_id is required"}), 400
    room_id = raw_room_id.strip()

    # check the file isn't empty
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        return jsonify({"error": "Empty file"}), 400

    # build a unique key so files don't overwrite each other
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
        # make sure the room actually exists
        room_type = _resolve_room(conn, room_id)
        if room_type is None:
            return jsonify({"error": "Room not found"}), 404

        # figure out if it's a logged-in user or a guest uploading
        user_id = session.get('user_id')
        guest_id = None
        if not user_id:
            guest_id = get_or_create_guest(conn, room_id)

        # actually push the file to r2
        try:
            r2 = get_r2_client()
            r2.upload_fileobj(Fileobj=file.stream, Bucket=bucket_name, Key=file_key)
        except Exception as e:
            app.logger.error(f"R2 upload failed: room_id={room_id} key={file_key} error={e}")
            # log the failure to db if it's a user room
            if room_type == 'user':
                try:
                    insert_log(
                        conn, event_type='file_upload_failed',
                        room_id=room_id, user_id=user_id, guest_id=guest_id,
                        ip_address=request.remote_addr, message='R2 upload failed',
                    )
                    conn.commit()
                except Exception as log_err:
                    app.logger.error(f"Could not record file_upload_failed log: {log_err}")
                    conn.rollback()
            return jsonify({"error": "Upload to storage failed"}), 500

        # save the file record in db after a successful r2 upload
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO files (room_id, file_name, file_key, filepath, user_id, guest_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (room_id, original_filename, file_key, filepath, user_id, guest_id),
            )
            # only log activity for user rooms, not guest ones
            if room_type == 'user':
                insert_log(
                    conn, event_type='file_upload',
                    room_id=room_id, user_id=user_id, guest_id=guest_id,
                    ip_address=request.remote_addr, message=original_filename,
                )
            conn.commit()
        except psycopg2.Error as e:
            app.logger.error(f"DB insert for file record failed: room_id={room_id} error={e}")
            conn.rollback()
            return jsonify({"error": "Database error during file record creation"}), 500

        return jsonify({"message": "upload successful", "file_key": file_key}), 200
    finally:
        # always clean up cursor and connection
        if cur is not None:
            try: cur.close()
            except Exception: pass
        try: conn.close()
        except Exception: pass

        
@app.route('/', methods=['GET'])
def home_page():
    # just the landing page
    return render_template('index.html')

@app.route('/dashboard', methods = ['GET'])
def dashboard_render():
    # grab username from session to personalize the page
    user_name = session['username']
    context = {
        "name": user_name,
    }
    return render_template('dashboard.html', **context)

@app.route('/profile', methods = ['GET'])
def profile_render():
    user_name = session['username']
    
    conn = psycopg2.connect(DATABASE_URL)
    cur=conn.cursor()

    # fetch the user's email to display on profile
    cur.execute("select email from users where username = %s", (user_name,))
    user_email = cur.fetchone()
    context = {
        "name": user_name,
        "email": user_email
    }

    return render_template('profile.html', **context)

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

            # create the guest owner before the room row - guests table has no fk constraint
            owner_token = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO guests (guest_token, room_id) VALUES (%s, %s) RETURNING id",
                (owner_token, new_room_id),
            )
            owner_guest_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO guest_room (room_id, room_name, guest_owner_id, expires_at, is_active)
                VALUES (%s, %s, %s, %s, TRUE)
                """,
                (new_room_id, room_name, owner_guest_id, expires_at),
            )
            conn.commit()

            # store the owner token in session so they're recognized when they re-enter the room
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

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # hash the password before storing - never store plaintext
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        try:
            cur.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s)",
                (username, email, password_hash)
            )
            conn.commit()
            flash('Account created successfully!', 'success')
            return redirect(url_for('login'))
        except psycopg2.errors.UniqueViolation:
            # username or email is already taken
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
        username = request.form['username']
        password = request.form['password']

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        try:
            cur.execute("SELECT id, password_hash FROM users WHERE username = %s", (username,))
            result = cur.fetchone()

            # same error message for both cases so we don't leak which one is wrong
            if result is None:
                flash('Wrong username or password.', 'error')
                return redirect(url_for('login'))

            user_id, stored_hash = result

            if not bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
                flash('Wrong username or password.', 'error')
                return redirect(url_for('login'))

            # set session vars on successful login
            session['user_id'] = user_id
            session['username'] = username
            session['logged_in'] = True

            flash(f'Welcome {username}!', 'success')
            return redirect(url_for('dashboard_render'))

        finally:
            cur.close()
            conn.close()

    return render_template('login.html')

@app.route('/create-room', methods=['POST'])
def create_room():
    data = request.get_json()
    nickname = data.get('nickname')
    room_name = data.get('room_name')
    # generate a random 8-char alphanumeric room id
    characters = string.ascii_letters + string.digits
    room_id = ''.join(secrets.choice(characters) for _ in range(8))
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    try:
        owner_user_id = session.get('user_id') 
        # room expires after 45 minutes
        cur.execute("insert into rooms (room_id, room_name, owner_user_id, expires_at) values (%s, %s, %s, now()+ interval '45 minutes')", (room_id, room_name, owner_user_id))
        conn.commit()
    except Exception as e:
        abort(500)
    finally:
        cur.close()
        conn.close()
    return url_for('room',r_id=room_id)

@app.route('/room/<r_id>')
def room(r_id):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        # only fetch the room if it hasn't expired yet
        cur.execute("select room_name, expires_at from rooms where room_id = %s and expires_at > now()", (r_id,))

        room = cur.fetchone()

        # 404 if room doesn't exist or is expired
        if room is None:
            abort(404)

        return render_template('room.html', room_id=r_id)

    finally:
        cur.close()
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)