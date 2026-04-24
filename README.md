# Room Drop

A temporary, browser-based file sharing application that allows users to share files and text in isolated rooms identified by unique Room IDs.

## Overview

Room Drop logically groups users into private rooms managed by a backend server and database. Each room functions as an independent environment with real-time updates, automatic expiry, and secure file sharing.

## Features

- 🏠 **Room-based isolation** — Each room is identified by a unique Room ID
- 🔄 **Real-time sync** — Files and messages update live via WebSockets
- ⏰ **Auto-expiry** — Rooms and their data are cleaned up automatically
- 🔒 **Secure authentication** — Passwords hashed with bcrypt
- 👤 **Guest or member access** — Use instantly as a guest or sign up for higher limits
- 🌐 **Browser-based** — No installation required

## Tech Stack

- **Backend:** Flask (Python)
- **Database:** PostgreSQL (Supabase)
- **Frontend:** HTML, CSS, Vanilla JavaScript
- **Authentication:** bcrypt password hashing
- **Real-time:** WebSockets (planned)

## Project Structure
room-drop/
├── app.py              # Flask application
├── README.md
├── templates/
│   ├── index.html      # Landing page
│   ├── signup.html     # Sign up page
│   └── login.html      # Log in page (WIP)
└── static/             # (optional) static assets


## Prerequisites

- **Python 3.10+** — [Download here](https://www.python.org/downloads/) (make sure to check "Add Python to PATH" during installation)
- **A Supabase account** — [supabase.com](https://supabase.com)
- **VS Code** (recommended) with the Python extension

## Setup

### 1. Clone or download the project

```bash
git clone <your-repo-url>
cd room-drop
```

### 2. Install dependencies

```bash
pip install flask psycopg2-binary bcrypt
```

If `pip` isn't recognized, use:

```bash
python -m pip install flask psycopg2-binary bcrypt
```

### 3. Set up the database

In your Supabase project, go to **SQL Editor** and run:

```sql
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);
```

### 4. Configure the database connection

Open `app.py` and update the `DATABASE_URL` with your Supabase connection string:

```python
DATABASE_URL = "postgresql://<user>:<password>@<host>:5432/postgres"
```

You can find this in your Supabase dashboard under **Project Settings → Database → Connection String**.

### 5. Run the app

```bash
python app.py
```

The app will be available at:

- **Landing page:** http://localhost:5000
- **Sign up:** http://localhost:5000/signup
- **Log in:** http://localhost:5000/login

## Routes

| Route | Method | Description |
|---|---|---|
| `/` | GET | Landing page |
| `/signup` | GET, POST | Create a new account |
| `/login` | GET, POST | Log in to an existing account |

## Database Schema

### `users`

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PRIMARY KEY | Unique user ID |
| `username` | VARCHAR(50) UNIQUE | User's chosen username |
| `email` | VARCHAR(100) UNIQUE | User's email address |
| `password_hash` | TEXT | bcrypt-hashed password |

## Development Notes

- **Auto-reload:** Flask runs in debug mode, so any changes to `app.py` automatically restart the server. HTML/CSS changes just need a browser refresh.
- **Database connections:** Each request opens a fresh DB connection to avoid stale connection errors from Supabase's idle timeout.
- **Password security:** All passwords are hashed with bcrypt before storage. Plaintext passwords are never stored.

## Roadmap

- [x] User signup with bcrypt password hashing
- [ ] User login and session management
- [ ] Room creation and joining
- [ ] WebSocket-based real-time file sharing
- [ ] Text message sharing in rooms
- [ ] Automatic room expiry and cleanup
- [ ] Guest mode (no account required)
- [ ] Custom room names (for members)
- [ ] File size and quota enforcement

## Contributors

- **Jiya** — Sign up flow, frontend design
- **Teammate** — Log in flow, authentication

## Troubleshooting

**`ModuleNotFoundError: No module named 'flask'`**
Install the missing package: `pip install flask`

**`column "email" of relation "users" does not exist`**
Your database table is out of date. Run the SQL from the setup section to recreate it.

**`server closed the connection unexpectedly`**
Your Supabase connection timed out. Make sure you're using the latest `app.py` which opens a fresh connection per request.

**Can't stop the Flask server with Ctrl + C**
Click the trash icon in the VS Code terminal to kill it, then open a new terminal.

**Page not loading / "Site can't be reached"**
Check that the Flask server is actually running (look for `Running on http://127.0.0.1:5000` in the terminal) and use `http://` not `https://`.

## License

This project is built for educational purposes as part of a semester project.
