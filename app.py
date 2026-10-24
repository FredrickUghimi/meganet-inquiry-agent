from sentinel import create_app

app = create_app()

if __name__ == "__main__":
    # Local dev only. On Render, gunicorn serves `app:app`.
    app.run(debug=False, port=5000)
