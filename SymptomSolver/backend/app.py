from flask import Flask, request, jsonify
from flask_cors import CORS
import mysql.connector
from transformers import AutoTokenizer, AutoModel
import torch
import numpy as np
from scipy.spatial.distance import cosine
import os

app = Flask(__name__)
CORS(app, resources={
    r"/*": {
        "origins": ["http://localhost:4200"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})


# Database configuration
db_config = {
    "host": "35.193.142.184",
    "user": "root",
    "password": "KinjalIsAFruit",
    "database": "SymptomSolver"
}


# Initialize BioBERT
tokenizer = None
model = None


def load_biobert():
    global tokenizer, model
    if tokenizer is None or model is None:
        print("Loading BioBERT model...")
        tokenizer = AutoTokenizer.from_pretrained("dmis-lab/biobert-v1.1", cache_dir="./models")
        model = AutoModel.from_pretrained("dmis-lab/biobert-v1.1", cache_dir="./models")
        print("BioBERT model loaded successfully")


def get_bert_embedding(text):
    """Get BioBERT embeddings for input text."""
    if tokenizer is None or model is None:
        load_biobert()
   
    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=512)
    with torch.no_grad():
        outputs = model(**inputs)
    return outputs.last_hidden_state.mean(dim=1).squeeze().numpy()


def preprocess_description(description, known_symptoms):
    """Preprocess text using symptoms from database."""
    text = description.lower()
   
    # Split into phrases
    phrases = [p.strip() for p in text.split("and")]
   
    # Get embeddings for each phrase
    phrase_embeddings = [get_bert_embedding(phrase) for phrase in phrases]
   
    # For each known symptom, find the best matching phrase
    processed_phrases = []
    for phrase, phrase_emb in zip(phrases, phrase_embeddings):
        best_match = None
        best_similarity = 0
       
        for symptom in known_symptoms:
            symptom_emb = get_bert_embedding(symptom)
            similarity = 1 - cosine(phrase_emb, symptom_emb)
           
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = symptom
       
        if best_similarity > 0.7:  # Only use matches above threshold
            processed_phrases.append(best_match)
        else:
            processed_phrases.append(phrase)
   
    return " and ".join(processed_phrases)


def map_description_to_symptoms(description, known_symptoms):
    """Map free text description to known symptoms using BioBERT embeddings."""
    # First preprocess the description
    processed_description = preprocess_description(description, known_symptoms)
    description_embedding = get_bert_embedding(processed_description)
   
    # Split description into individual phrases
    phrases = [p.strip() for p in processed_description.lower().split("and")]
   
    matches = []
    for symptom in known_symptoms:
        best_similarity = 0
        # Compare each phrase with the symptom
        for phrase in phrases:
            symptom_embedding = get_bert_embedding(symptom)
            similarity = 1 - cosine(description_embedding, symptom_embedding)
            best_similarity = max(best_similarity, similarity)
           
        # Only include if similarity is very high
        if best_similarity > 0.75:  # Increased threshold
            matches.append({
                "symptom": symptom,
                "confidence": float(best_similarity)
            })
   
    # Sort by confidence and take only very strong matches
    matches.sort(key=lambda x: x["confidence"], reverse=True)
   
    # Filter matches by checking if key words from symptom appear in description
    filtered_matches = []
    for match in matches[:10]:  # Look at top 10 potential matches
        symptom_words = set(match["symptom"].lower().split())
        desc_words = set(processed_description.lower().split())
       
        # If any key word from symptom appears in description
        word_overlap = symptom_words.intersection(desc_words)
        if word_overlap or match["confidence"] > 0.85:  # Very high confidence can bypass word match
            filtered_matches.append(match["symptom"])
   
    return filtered_matches[:5]  # Return top 5 filtered matches


# Rest of your code remains the same...


@app.route("/api/process-description", methods=["POST", "OPTIONS"])
def process_description():
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add('Access-Control-Allow-Origin', 'http://localhost:4200')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST')
        return response


    data = request.json
    description = data.get("description")
   
    if not description:
        return jsonify({"error": "No description provided"}), 400
   
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
       
        # Get known symptoms from database
        cursor.execute("SELECT DISTINCT SymptomName FROM KnownSymptoms")
        known_symptoms = [row[0] for row in cursor.fetchall()]
       
        # Map description to symptoms
        matched_symptoms = map_description_to_symptoms(description, known_symptoms)
       
        return jsonify({
            "matched_symptoms": matched_symptoms
        })
       
    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 500
       
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/diagnosis", methods=["POST", "OPTIONS"])
def get_diagnosis():
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST, OPTIONS')
        return response


    data = request.json
    username = data.get("username")
    first_name = data.get("firstName")
    last_name = data.get("lastName")
    age = data.get("age")
    gender = data.get("gender")
    symptoms = data.get("symptoms", [])


    conn = None
    cursor = None


    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)


        # Check if user exists
        cursor.execute("SELECT * FROM Patient WHERE Username = %s", (username,))
        existing_user = cursor.fetchone()


        if not existing_user:
            # Insert new user
            cursor.execute(
                """
                INSERT INTO Patient (Username, FirstName, LastName, Gender, Age)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (username, first_name, last_name, gender, age)
            )
            conn.commit()
        else:
            # Update existing user
            cursor.execute(
                """
                UPDATE Patient
                SET FirstName = %s, LastName = %s, Gender = %s, Age = %s
                WHERE Username = %s
                """,
                (first_name, last_name, gender, age, username)
            )
            conn.commit()


        # Process diagnoses
        diagnoses_with_meds = []
        for symptom in symptoms:
            cursor.execute(
                """
                SELECT DISTINCT d.DiseaseName, d.SymptomGroupId
                FROM HasSymptom hs
                JOIN KnownSymptoms ks ON hs.SymptomIndex = ks.SymptomIndex
                JOIN Diagnosis d ON d.SymptomGroupId = hs.SymptomGroupId
                WHERE ks.SymptomName = %s
                """,
                (symptom,)
            )
            diagnoses = cursor.fetchall()


            for diagnosis in diagnoses:
                # Add to HasDiagnosis
                cursor.execute(
                    """
                    INSERT IGNORE INTO HasDiagnosis (Username, SymptomGroupId)
                    VALUES (%s, %s)
                    """,
                    (username, diagnosis['SymptomGroupId'])
                )


                # Get medications
                cursor.execute(
                    """
                    SELECT MedicationName, Prescription
                    FROM Medication
                    WHERE SymptomGroupId = %s
                    """,
                    (diagnosis['SymptomGroupId'],)
                )
                medications = cursor.fetchall()


                # Check for conflicts
                cursor.execute(
                    """
                    SELECT GROUP_CONCAT(m.MedicationName SEPARATOR ', ')
                    FROM Medication m
                    WHERE m.SymptomGroupId = %s
                    AND m.MedicationName IN (
                        SELECT mp.AllergicMedication
                        FROM MedicalProfile mp
                        JOIN HasProfile hp ON mp.ProfileIndex = hp.ProfileIndex
                        WHERE hp.Username = %s
                    )
                    """,
                    (diagnosis['SymptomGroupId'], username)
                )
                conflict = cursor.fetchone()['GROUP_CONCAT(m.MedicationName SEPARATOR \', \')']


                diagnoses_with_meds.append({
                    "disease": diagnosis['DiseaseName'],
                    "medications": [{"name": med['MedicationName'], "prescription": med['Prescription']}
                                  for med in medications],
                    "conflict": conflict
                })


        conn.commit()


        response = jsonify({
            "diagnosis": diagnoses_with_meds,
            "data": {
                "username": username,
                "firstName": first_name,
                "lastName": last_name,
                "age": age,
                "gender": gender,
                "symptoms": symptoms
            }
        })
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response


    except mysql.connector.Error as err:
        print(f"Database error: {str(err)}")
        error_response = jsonify({"error": str(err), "message": "An error occurred"})
        error_response.headers.add('Access-Control-Allow-Origin', '*')
        return error_response, 500


    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


##for transaction 2
@app.route("/api/check-conflicts", methods=["POST", "OPTIONS"])
def check_conflicts():
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add('Access-Control-Allow-Origin', 'http://localhost:4200')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST')
        return response


    data = request.json
    username = data.get("username")
    symptoms = data.get("symptoms", [])


    if not username or not symptoms:
        return jsonify({"error": "Invalid input"}), 400


    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()


        cursor.execute("START TRANSACTION;")


        # Get ProfileIndex
        cursor.execute("""
            SELECT ProfileIndex
            FROM HasProfile
            WHERE Username = %s
            LIMIT 1;
        """, (username,))
        profile_result = cursor.fetchone()
       
        if not profile_result:
            cursor.execute("ROLLBACK;")
            return jsonify({"error": "User profile not found"}), 404
           
        profile_index = profile_result[0]


        # Get SymptomGroupIds for the symptoms
                # Get symptom indices first
        placeholders = ', '.join(['%s'] * len(symptoms))
        cursor.execute(f"""
            SELECT DISTINCT hs.SymptomGroupId
            FROM HasSymptom hs
            JOIN KnownSymptoms ks ON hs.SymptomIndex = ks.SymptomIndex
            WHERE ks.SymptomName IN ({placeholders})
            GROUP BY hs.SymptomGroupId
        """, tuple(symptoms))
       
        symptom_group_ids = [row[0] for row in cursor.fetchall()]


        # Check for conflicts
        conflicts = []
        for group_id in symptom_group_ids:
            cursor.execute("""
                SELECT GROUP_CONCAT(m.MedicationName SEPARATOR ', ')
                FROM Medication m
                WHERE m.SymptomGroupId = %s
                AND m.MedicationName IN (
                    SELECT mp.AllergicMedication
                    FROM MedicalProfile mp
                    WHERE mp.ProfileIndex = %s
                );
            """, (group_id, profile_index))
           
            conflict = cursor.fetchone()[0]
            if conflict:
                conflicts.append(conflict)


        cursor.execute("COMMIT;")


        if conflicts:
            message = f"Warning: The following medications conflict with your allergies: {', '.join(conflicts)}. Please consult your doctor for alternatives."
        else:
            message = "No conflicts detected between recommended medications and your allergies."


        return jsonify({"message": message})


    except Exception as e:
        if cursor:
            cursor.execute("ROLLBACK;")
        return jsonify({"error": str(e)}), 500


    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# Flask endpoint
@app.route("/api/delete-account", methods=["POST", "OPTIONS"])
def delete_account():
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST, OPTIONS')
        return response


    data = request.json
    username = data.get("username")
   
    conn = None
    cursor = None


    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()


        # Delete from HasDiagnosis first
        cursor.execute("DELETE FROM HasDiagnosis WHERE Username = %s", (username,))
       
        # Delete from HasProfile
        cursor.execute("DELETE FROM HasProfile WHERE Username = %s", (username,))
       
        # Finally delete from Patient
        cursor.execute("DELETE FROM Patient WHERE Username = %s", (username,))
       
        conn.commit()


        response = jsonify({"message": "Account deleted successfully"})
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response


    except mysql.connector.Error as err:
        if cursor:
            cursor.execute("ROLLBACK")
        error_response = jsonify({"error": str(err), "message": "Failed to delete account"})
        error_response.headers.add('Access-Control-Allow-Origin', '*')
        return error_response, 500


    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
       
@app.route("/test-db-structure")
def test_db_structure():
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
       
        # Get all tables
        cursor.execute("SHOW TABLES")
        tables = cursor.fetchall()
       
        structure = {}
        for table in tables:
            table_name = table[0]
            cursor.execute(f"DESCRIBE {table_name}")
            columns = cursor.fetchall()
            structure[table_name] = columns
           
        return jsonify(structure)
       
    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


@app.route("/test-queries", methods=["GET"])
def test_queries():
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
       
        # Test 1: Check HasSymptom mappings
        cursor.execute("""
            SELECT hs.SymptomGroupId, ks.SymptomName
            FROM HasSymptom hs
            JOIN KnownSymptoms ks ON hs.SymptomIndex = ks.SymptomIndex
            LIMIT 5
        """)
        symptom_mappings = cursor.fetchall()
       
        # Test 2: Check Diagnosis table
        cursor.execute("""
            SELECT SymptomGroupId, DiseaseName
            FROM Diagnosis
            LIMIT 5
        """)
        diagnoses = cursor.fetchall()
       
        # Test 3: Check Medication table
        cursor.execute("""
            SELECT SymptomGroupId, MedicationName, Prescription
            FROM Medication
            LIMIT 5
        """)
        medications = cursor.fetchall()
       
        return jsonify({
            "symptom_mappings": symptom_mappings,
            "diagnoses": diagnoses,
            "medications": medications
        })
       
    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()




@app.route("/debug-symptom/<symptom_name>")
def debug_symptom(symptom_name):
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
       
        # Check if symptom exists
        cursor.execute(
            """
            SELECT SymptomIndex, SymptomName
            FROM KnownSymptoms
            WHERE SymptomName = %s
            """,
            (symptom_name,)
        )
        symptom = cursor.fetchone()
       
        if symptom:
            symptom_index = symptom[0]
           
            # Get HasSymptom mapping
            cursor.execute(
                """
                SELECT SymptomGroupId
                FROM HasSymptom
                WHERE SymptomIndex = %s
                """,
                (symptom_index,)
            )
            group_ids = cursor.fetchall()
           
            # Get diagnoses for these groups
            if group_ids:
                group_id_list = [g[0] for g in group_ids]
                placeholders = ','.join(['%s'] * len(group_id_list))
                cursor.execute(
                    f"""
                    SELECT DISTINCT DiseaseName
                    FROM Diagnosis
                    WHERE SymptomGroupId IN ({placeholders})
                    """,
                    tuple(group_id_list)
                )
                diagnoses = cursor.fetchall()
            else:
                diagnoses = []
           
            return jsonify({
                "symptom_found": symptom,
                "group_ids": group_ids,
                "related_diagnoses": diagnoses
            })
       
        return jsonify({"error": "Symptom not found"})
       
    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
@app.route("/debug-medications", methods=["GET"])
def debug_medications():
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
       
        # Get all medications with their symptom group IDs
        cursor.execute("""
            SELECT SymptomGroupId, MedicationName, Prescription
            FROM Medication
            LIMIT 50
        """)
        medications = cursor.fetchall()
       
        return jsonify({
            "medications": [
                {
                    "symptom_group_id": med[0],
                    "medication_name": med[1],
                    "prescription": med[2]
                } for med in medications
            ]
        })
       
    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


@app.route("/debug-diagnoses", methods=["GET"])
def debug_diagnoses():
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
       
        # Get diagnoses with their symptom group IDs
        cursor.execute("""
            SELECT SymptomGroupId, DiseaseName
            FROM Diagnosis
            LIMIT 50
        """)
        diagnoses = cursor.fetchall()
       
        return jsonify({
            "diagnoses": [
                {
                    "symptom_group_id": diag[0],
                    "disease_name": diag[1]
                } for diag in diagnoses
            ]
        })
       
    except mysql.connector.Error as err:
        return jsonify({"error": str(err)}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/diagnosis-with-top-symptoms", methods=["POST", "OPTIONS"])
def get_diagnosis_with_top_symptoms():
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST')
        return response

    data = request.json
    username = data.get("username")
    first_name = data.get("firstName")
    last_name = data.get("lastName")
    age = data.get("age")
    gender = data.get("gender")
    symptoms = data.get("symptoms", [])

    conn = None
    cursor = None

    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()

        # Patient insert
        cursor.execute(
            """
            INSERT INTO Patient (Username, FirstName, LastName, Gender, Age)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE FirstName=%s, LastName=%s, Gender=%s, Age=%s
            """,
            (username, first_name, last_name, gender, age, first_name, last_name, gender, age)
        )
        conn.commit()

        # HasDiagnosis insert
        for symptom in symptoms:
            cursor.execute(
                """
                INSERT INTO HasDiagnosis (Username, SymptomGroupId)
                SELECT %s, hs.SymptomGroupId
                FROM HasSymptom hs
                JOIN KnownSymptoms ks ON hs.SymptomIndex = ks.SymptomIndex
                WHERE ks.SymptomName = %s
                ON DUPLICATE KEY UPDATE SymptomGroupId=hs.SymptomGroupId
                """,
                (username, symptom)
            )
        conn.commit()

        # Get diagnoses
        cursor.execute(
            """
            SELECT DISTINCT d.DiseaseName, d.SymptomGroupId
            FROM Diagnosis d
            JOIN HasDiagnosis hd ON d.SymptomGroupId = hd.SymptomGroupId
            WHERE hd.Username = %s
            """,
            (username,)
        )
        diagnoses = cursor.fetchall()

        diagnoses_with_meds = []
        for disease_name, symptom_group_id in diagnoses:
            cursor.execute(
                """
                SELECT MedicationName, Prescription
                FROM Medication
                WHERE SymptomGroupId = %s
                """,
                (symptom_group_id,)
            )
            medications = [{"name": med[0], "prescription": med[1]} for med in cursor.fetchall()]
            
            diagnoses_with_meds.append({
                "disease": disease_name,
                "medications": medications
            })

        # Get top 3 symptoms
        cursor.callproc("GetTop3SymptomsForPatient", [username])
        top_symptoms = []
        for result in cursor.stored_results():
            top_symptoms.extend(result.fetchall())

        response = {
            "diagnosis": diagnoses_with_meds,
            "data": {
                "username": username,
                "firstName": first_name,
                "lastName": last_name,
                "age": age,
                "gender": gender,
                "symptoms": symptoms
            },
            "top_symptoms": top_symptoms
        }

        return jsonify(response)

    except mysql.connector.Error as err:
        print(f"Database error: {str(err)}")
        return jsonify({
            "error": str(err),
            "message": "An error occurred while processing your request"
        }), 500

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/api/gender-disease-comparison", methods=["GET", "OPTIONS"])
def compare_diseases_by_gender():
    if request.method == "OPTIONS":
        response = jsonify({"status": "ok"})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'GET')
        return response

    username = request.args.get('username')
    if not username:
        return jsonify({"error": "Username is required"}), 400

    conn = None
    cursor = None

    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        # Get user's gender
        cursor.execute(
            "SELECT Gender FROM Patient WHERE Username = %s",
            (username,)
        )
        user = cursor.fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404

        user_gender = user['Gender']

        # Get disease comparison
        cursor.execute("""
            SELECT 
                d.DiseaseName,
                COUNT(CASE WHEN p.Gender = %s THEN 1 END) as same_gender_count,
                COUNT(CASE WHEN p.Gender != %s THEN 1 END) as other_gender_count,
                COUNT(*) as total_count
            FROM Patient p
            JOIN HasDiagnosis hd ON p.Username = hd.Username
            JOIN Diagnosis d ON hd.SymptomGroupId = d.SymptomGroupId
            GROUP BY d.DiseaseName
            HAVING same_gender_count > 0 OR other_gender_count > 0
            ORDER BY total_count DESC
            LIMIT 10
        """, (user_gender, user_gender))

        comparisons = cursor.fetchall()

        response = jsonify({
            "userGender": user_gender,
            "comparisons": comparisons
        })
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response

    except mysql.connector.Error as err:
        error_response = jsonify({"error": str(err), "message": "Database error occurred"})
        error_response.headers.add('Access-Control-Allow-Origin', '*')
        return error_response, 500

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
           
@app.route("/")
def home():
    return "Test for if backend is running!"


if __name__ == "__main__":
    os.makedirs("./models", exist_ok=True)
    load_biobert()
    app.run(debug=True)
