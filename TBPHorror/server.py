from flask import Flask, request, render_template, jsonify, redirect, url_for, flash, session
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, login_user, login_required, logout_user, UserMixin, current_user
from neo4j import GraphDatabase
from config import Config
# from waitress import serve
driver = GraphDatabase.driver(Config.NEO4J_URI, auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD))

app = Flask(__name__)
app.secret_key = 'key'
login_manager = LoginManager(app)
login_manager.login_view = "login"



class User(UserMixin):
    def __init__(self, name):
        self.id = name

    @staticmethod
    def find_user_by_name(name):
        with driver.session() as session:
            query = "MATCH (u:user {Name: $name}) RETURN u"
            result = session.run(query, name=name)
            record = result.single()
            return record["u"] if record else None

    @staticmethod
    def validate_password(name, password):
        user = User.find_user_by_name(name)
        if user and user["password"] == password:
            return True
        return False


@login_manager.user_loader
def load_user(name):
    user = User.find_user_by_name(name)
    if user:
        return User(name=user["Name"])  
    return None


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        password = request.form['password']
        #hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

        with driver.session() as session:
            query = "CREATE (u:user {Name: $name, password: $password})"
            session.run(query, name=name, password=password)
        flash("Registration successful! Please log in.", "success")
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = request.form['name']
        password = request.form['password']

        if User.validate_password(name, password):
            user = User(name=name)
            login_user(user)
            flash("Login successful!", "success")
            return redirect(url_for('index'))
        else:
            flash("Invalid name or password.", "danger")
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    def get_movies(tx, user_name):
        query = """
        MATCH (vu:user {Name:$user_name})-[:LIKES]->(cross_movie:Movie)<-[:LIKES]-(u:user)-[:LIKES]->(other_movie:Movie)
        WHERE NOT (vu)-[:LIKES]->(other_movie)
        WITH other_movie.title as title, other_movie.id as id, other_movie.genres as genres, other_movie.overview as overview,
             other_movie.popularity as popularity, other_movie.release_date as release_date, count(u) as likes
        RETURN title, id, genres, overview, popularity, release_date, likes
        ORDER BY likes DESC LIMIT 10
        """
        return [record for record in tx.run(query, user_name=user_name)]

    user_name = current_user.id

    with driver.session() as session:
        results = session.execute_read(get_movies, user_name=user_name)

    movies = [
        {
            "id": movie["id"],
            "title": movie["title"],
            "genres": movie["genres"],
            "overview": movie["overview"],
            "popularity": movie["popularity"],
            "release_date": movie["release_date"],
            "likes": movie["likes"]
        }
        for movie in results
    ]

    return render_template('index.html', movies=movies)


@app.route('/search', methods=['POST'])
def search():
    movie_title = request.form.get('movie')

    def find_movies(tx, title):
        query = """
        WITH $title AS inputMovieTitle
        
        MATCH (m1:Movie {title: inputMovieTitle})-[:HAS_GENRE]->(g:Genre)
        WITH m1, m1.overview AS inputOverview, COLLECT(g.name) AS inputGenres
        
        MATCH (m2:Movie)-[:HAS_GENRE]->(g2:Genre)
        WHERE m1 <> m2 AND m2.overview IS NOT NULL AND g2.name IN inputGenres
        WITH m1, m2, inputOverview, m2.overview AS candidateOverview, 
             COLLECT(DISTINCT g2.name) AS sharedGenres, COUNT(DISTINCT g2) AS sharedGenresCount
        
        WITH m1, m2, sharedGenres, sharedGenresCount, 
             apoc.text.jaroWinklerDistance(inputOverview, candidateOverview) AS similarity
        WHERE similarity > 0.2
        
        RETURN m2.title AS RecommendedMovie, similarity, sharedGenresCount, sharedGenres
        ORDER BY similarity DESC, sharedGenresCount DESC
        LIMIT 10
        """
        return list(tx.run(query, title=title))
    

    
    with driver.session() as session:
        results = session.execute_read(find_movies, movie_title)

    
    recommendations = []
    for record in results:
        recommendations.append({
            "RecommendedMovie": record["RecommendedMovie"],
            "Similarity": record["similarity"],
            "SharedGenresCount": record["sharedGenresCount"],
            "SharedGenres": record["sharedGenres"]
        })

    
    return render_template('recommendations.html', movies=recommendations)

@app.route('/like/<title>', methods=['POST'])
@login_required
def like_movie(title):
    
    def add_like(tx, user_name, title):
        query = """
        MATCH (u:user {Name: $user_name}), (m:Movie {title: $title})
        MERGE (u)-[:LIKES]->(m)
        RETURN m.title AS movie_title
        """
        result = tx.run(query, user_name=user_name, title=title)
        record = result.single()
        return record["movie_title"] if record else None

    user_name = current_user.id 
    try:
        with driver.session() as session:
            liked_movie = session.execute_write(add_like, user_name=user_name, title=title)

        if liked_movie:
            flash(f"You liked the movie: '{liked_movie}'. Enjoy your spooky adventure!", "success")
        else:
            flash("Failed to like the movie. It might not exist.", "danger")
    except Exception as e:
        app.logger.error(f"Error liking movie '{title}' for user '{user_name}': {e}")
        flash("An error occurred. Please try again.", "danger")

    return redirect(url_for('all_movies'))

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/add_movie', methods=['GET', 'POST'])
@login_required
def add_movie():
    def create(tx, title, genres, overview, popularity, release_date):
        query = """
        CREATE (m:Movie {
            title: $title,
            genres: $genres,
            overview: $overview,
            popularity: $popularity,
            release_date: $release_date
        })
        RETURN m.id AS id, m.title AS title
        """
        return tx.run(query, title=title, genres=genres, overview=overview, popularity=popularity, release_date=release_date)
    if request.method == 'POST':
        title = request.form['title']
        genres = request.form['genres']
        overview = request.form['overview']
        popularity = request.form['popularity']
        release_date = request.form['release_date']

        with driver.session() as session:
            result = session.execute_write(create, title, genres, overview, popularity,release_date)

        flash('Movie added!', 'success')
        return redirect(url_for('all_movies'))
    return render_template('add_movie.html')

@app.route('/all_movies', methods=['GET', 'POST'])
@login_required
def all_movies():
    def get_all_movies(tx, search_query=None):
        if search_query:
            query = """
            MATCH (m:Movie)
            WHERE m.title CONTAINS $search_query
            RETURN m.id AS id, m.title AS title, m.genres AS genres, m.overview AS overview, 
                   m.popularity AS popularity, m.release_date AS release_date
            ORDER BY m.popularity DESC
            """
            return [record for record in tx.run(query, search_query=search_query)]
        else:
            query = """
            MATCH (m:Movie)
            RETURN m.id AS id, m.title AS title, m.genres AS genres, m.overview AS overview, 
                   m.popularity AS popularity, m.release_date AS release_date
            ORDER BY m.popularity DESC LIMIT 50
            """
            return [record for record in tx.run(query)]

    
    search_query = None
    if request.method == 'POST':
        search_query = request.form.get('search_query')

    with driver.session() as session:
        results = session.execute_read(get_all_movies, search_query=search_query)

    
    movies = [
        {
            "id": movie["id"],
            "title": movie["title"],
            "genres": movie["genres"],
            "overview": movie["overview"],
            "popularity": movie["popularity"],
            "release_date": movie["release_date"],
        }
        for movie in results
    ]

    return render_template('movies.html', movies=movies)


@app.teardown_appcontext
def close_driver(exception=None):
    
    if driver:
        driver.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
