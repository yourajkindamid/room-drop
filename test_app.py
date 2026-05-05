from flask import Flask, render_template, request, redirect, url_for, flash,session,abort
import psycopg2
import bcrypt
import string
import secrets

DATABASE_URL = "postgresql://postgres.fzuoyaxlwwsanlsicmoo:hosh_me_aao_abhijeet_69@aws-1-ap-south-1.pooler.supabase.com:5432/postgres"
app = Flask(__name__)
app.secret_key = 'diddy_blud_managment_system'

@app.route('/', methods=['GET'])
def home_page():
    return render_template('index.html')

@app.route('/dashboard', methods = ['GET'])
def dashboard_render():

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

            # Create the owner guest first; guests.room_id has no FK so this
            # is fine even though the guest_room row doesn't exist yet.
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

            # Put the owner's token in the session so they're recognized
            # as the same guest when they enter the room page.
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

            if result is None:
                flash('Wrong username or password.', 'error')
                return redirect(url_for('login'))

            user_id, stored_hash = result

            if not bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
                flash('Wrong username or password.', 'error')
                return redirect(url_for('login'))

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
    characters = string.ascii_letters + string.digits
    room_id = ''.join(secrets.choice(characters) for _ in range(8))
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    try:
        owner_user_id = session.get('user_id') 
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
        cur.execute("select room_name, expires_at from rooms where room_id = %s and expires_at > now()", (r_id,))

        room = cur.fetchone()

        if room is None:
            abort(404)

        return render_template('room.html', room_id=r_id)

    finally:
        cur.close()
        conn.close()
if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)