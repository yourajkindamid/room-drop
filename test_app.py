from flask import Flask,render_template
import psycopg2,bcrypt
conn=psycopg2.connect("postgresql://postgres.fzuoyaxlwwsanlsicmoo:hosh_me_aao_abhijeet_69@aws-1-ap-south-1.pooler.supabase.com:5432/postgres")
cur=conn.cursor()
app=Flask('__name__')
app.secret_key='diddy_blud_managment_system'
@app.route('/',methods=['GET'])
def home_page():
        return render_template('index.html')
if __name__ == '__main__':
    app.run(host='0.0.0.0')
print("hello")