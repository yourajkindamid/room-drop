from flask import Flask, render_template, request, redirect, url_for, flash
import psycopg2
import bcrypt

DATABASE_URL = "postgresql://postgres.fzuoyaxlwwsanlsicmoo:hosh_me_aao_abhijeet_69@aws-1-ap-south-1.pooler.supabase.com:5432/postgres"

app = Flask(__name__)
app.secret_key = 'diddy_blud_managment_system'


@app.route('/', methods=['GET'])
def home_page():
    return render_template('index.html')

@app.route('/test', methods = ['GET'])
def next_stage():
    return render_template('test.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']

        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
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

        password_hash1 = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        try:
            cur.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
            result = cur.fetchone()

            if result is None or not bcrypt.checkpw(password.encode('utf-8'), result[0].encode('utf-8')):
                flash('Wrong username or password.', 'error')
                return redirect(url_for('login'))
            else:
                flash(f'Account was logged in less gooo! Welcome {username}!', 'success')
                return redirect(url_for('next_stage'))
        finally:
            cur.close()
            conn.close()

    return render_template('login.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)