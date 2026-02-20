from flask import Blueprint, request, jsonify
from datetime import datetime
import json
import os
import sys
from pathlib import Path
import pytz
import tempfile

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.mongodb_service import scheduled_interviews
from services.drive_service import upload_to_drive
from utils.helpers import parse_iso_datetime

interviews_bp = Blueprint('interviews', __name__, url_prefix='/api/interviews')

# Initialize OpenAI lazily to avoid import errors
client = None

def get_openai_client():
    """
    Initialize OpenAI client with proper configuration to avoid proxy issues.
    """
    global client
    if client is None:
        try:
            from openai import OpenAI
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY not set in environment variables")
            
            # Create client with explicit configuration to avoid proxy parameter issues
            # This works with openai>=1.0.0
            client = OpenAI(
                api_key=api_key,
                timeout=60.0,
                max_retries=2
            )
            print("✅ OpenAI client initialized successfully")
        except Exception as e:
            print(f"❌ Error initializing OpenAI: {e}")
            print(f"   Make sure you have: pip install openai>=1.0.0")
            raise
    return client

UTC = pytz.utc
IST = pytz.timezone("Asia/Kolkata")

SYSTEM_PROMPT = """
You are an expert technical interviewer.
Generate clear, concise interview questions.
Ask only one question at a time.
Make questions specific to the job description provided.
"""

# In-memory storage for current interview session

@interviews_bp.route('/generate-questions', methods=['POST'])
def generate_questions():
    """Generate interview questions based on job description"""
    try:
        data = request.json
        jd_text = data.get("jd", "").strip()
        interview_id = data.get("interview_id")

        if not jd_text:
            return jsonify({"error": "Job description required"}), 400

        if not interview_id:
            return jsonify({"error": "Interview ID required"}), 400

        # 🔐 Interview status validation & atomic lock
        from services.mongodb_service import scheduled_interviews

        update_result = scheduled_interviews.find_one_and_update(
            {
                "interview_id": interview_id,
                "interview_status": "scheduled"
            },
            {
                "$set": {
                    "interview_status": "started",
                    "started_at": datetime.utcnow()
                }
            }
        )

        if not update_result:
            existing = scheduled_interviews.find_one({"interview_id": interview_id})
            
            if existing:
                current_status = existing.get("interview_status")
                if current_status == "started":
                    return jsonify({
                        "status": "already_started",
                        "message": "Interview already in progress from another session"
                    }), 403
                elif current_status == "completed":
                    return jsonify({
                        "status": "completed",
                        "message": "Interview link already used or invalid"
                    }), 403
            
            return jsonify({
                "status": "expired",
                "message": "Interview link already used or invalid"
            }), 403

        prompt = f"""
You are an AI Technical Interviewer.

Your task is to generate interview questions STRICTLY based on the provided Job Description (JD).
Do NOT assume any skills, tools, experience, or background that is NOT explicitly mentioned in the JD.

=====================
RULES & CONSTRAINTS
=====================

1. Use ONLY the information present in the Job Description.
   - If a skill, tool, framework, or technology is not mentioned, do NOT ask about it.
   - Do NOT assume the candidate has prior industry experience unless the JD clearly states it.

2. Experience Handling:
   - If the JD mentions "years of experience":
     • 0–1 years → Beginner / foundational questions
     • 1–3 years → Intermediate, hands-on, practical questions
     • 3–5 years → Advanced problem-solving and optimization questions
     • 5+ years → System design, decision-making, trade-offs, scalability questions
   - If experience is NOT mentioned, ask neutral, role-relevant questions without increasing difficulty.

3. Question Style:
   - Questions must be clear, conversational, and realistic (as asked by a human interviewer).
   - Avoid theoretical definitions unless the JD explicitly requires fundamentals.
   - Prefer scenario-based or practical questions when experience > 1 year.

4. Question Coverage:
   - Focus on:
     • Core skills mentioned in the JD
     • Day-to-day responsibilities implied by the JD
     • Real-world usage of the mentioned technologies
   - Do NOT include:
     • Company-specific questions
     • HR or behavioral questions
     • Questions unrelated to the JD

5. Quantity & Format:
   - Generate EXACTLY 5 questions.
   - Return ONLY the numbered questions (1 to 5).
   - One question per line.
   - Do NOT add explanations, headings, or extra text.

=====================
JOB DESCRIPTION
=====================
{jd_text}
"""

        openai_client = get_openai_client()
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.4
        )

        raw_text = response.choices[0].message.content
        questions = parse_questions(raw_text)

        scheduled_interviews.update_one(
            {"interview_id": interview_id},
            {
                "$set": {
                    "questions": questions,
                    "current_index": 0,
                    "qna": [],
                    "interview_status": "in_progress"
                }
            }
        )


        return jsonify({
            "status": "success",
            "total": len(questions),
            "questions": questions
        }), 200

    except Exception as e:
        print(f"Error generating questions: {e}")
        return jsonify({"error": str(e)}), 500


@interviews_bp.route('/next-question/<interview_id>', methods=['GET'])
def next_question(interview_id):
    """Get next interview question"""
    try:
        from services.mongodb_service import scheduled_interviews

        interview = scheduled_interviews.find_one(
            {"interview_id": interview_id}
        )

        if not interview:
            return jsonify({"error": "Interview not found"}), 404

        questions = interview.get("questions", [])
        current_index = interview.get("current_index", 0)

        if current_index >= len(questions):
            return jsonify({"done": True, "question": ""}), 200

        question = questions[current_index]

        return jsonify({
            "done": False,
            "question": question,
            "questionNumber": current_index + 1,
            "totalQuestions": len(questions)
        }), 200

    except Exception as e:
        print(f"Error getting next question: {e}")
        return jsonify({"error": str(e)}), 500


@interviews_bp.route('/submit-answer/<interview_id>', methods=['POST'])
def submit_answer(interview_id):
    """Submit answer to a question"""
    try:
        from services.mongodb_service import scheduled_interviews

        data = request.json
        question = data.get("question")
        answer = data.get("answer")

        if not question or not answer:
            return jsonify({"error": "Question and answer required"}), 400

        # Step 1: Fetch current state
        interview = scheduled_interviews.find_one(
            {"interview_id": interview_id},
            {"current_index": 1, "questions": 1}
        )

        if not interview:
            return jsonify({"error": "Interview not found"}), 404

        current_index = interview.get("current_index", 0)
        questions = interview.get("questions", [])

        # Guard 1: Prevent answering beyond total questions
        if current_index >= len(questions):
            return jsonify({"error": "Interview already completed"}), 400

        # Guard 2: Ensure question matches expected question
        expected_question = questions[current_index]

        if question != expected_question:
            return jsonify({
                "error": "Invalid question submission (possible duplicate or out-of-order request)"
            }), 409

        # Step 2: Atomic update with index condition
        result = scheduled_interviews.update_one(
            {
                "interview_id": interview_id,
                "current_index": current_index  # critical guard condition
            },
            {
                "$push": {
                    "qna": {
                        "question": question,
                        "answer": answer
                    }
                },
                "$inc": {
                    "current_index": 1
                }
            }
        )

        if result.modified_count == 0:
            # Means another request already incremented index
            return jsonify({
                "error": "Duplicate or concurrent submission detected"
            }), 409

        print(f"✅ Answer saved for interview {interview_id}")

        return jsonify({
            "status": "success",
            "message": "Answer recorded"
        }), 200

    except Exception as e:
        print(f"Error submitting answer: {e}")
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# ✅ NEW: Whisper STT endpoint — replaces browser SpeechRecognition
# Frontend records audio, sends it here, gets back a transcript.
# TTS stays entirely in the browser (speechSynthesis) — zero OpenAI TTS cost.
# ─────────────────────────────────────────────────────────────────────────────
@interviews_bp.route('/transcribe', methods=['POST'])
def transcribe_audio():
    """
    Transcribe candidate's spoken answer using OpenAI Whisper.
    Expects a multipart/form-data POST with:
        audio  — audio blob (webm/ogg/wav, anything Whisper accepts)
    """
    try:
        if 'audio' not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files['audio']

        # Whisper needs a real file on disk (or a file-like with a name)
        suffix = '.webm'
        original_filename = audio_file.filename or ''
        if '.' in original_filename:
            suffix = '.' + original_filename.rsplit('.', 1)[-1]

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            audio_file.save(tmp.name)
            tmp_path = tmp.name

        try:
            openai_client = get_openai_client()
            with open(tmp_path, 'rb') as f:
                transcript_response = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language="en"
                )
            transcript = transcript_response.text.strip()
        finally:
            os.unlink(tmp_path)  # always clean up temp file

        print(f"✅ Whisper transcript: {transcript[:80]}...")

        return jsonify({
            "status": "success",
            "transcript": transcript
        }), 200

    except Exception as e:
        print(f"❌ Transcription error: {e}")
        return jsonify({"error": str(e)}), 500


@interviews_bp.route('/evaluate/<interview_id>', methods=['GET'])
def evaluate_interview(interview_id):
    """Get AI evaluation of interview"""
    try:

        interview = scheduled_interviews.find_one(
            {"interview_id": interview_id}
        )

        if not interview:
            return jsonify({"error": "Interview not found"}), 404

        qna = interview.get("qna", [])

        if not qna:
            return jsonify({"error": "No interview data"}), 400
        combined_text = ""
        for idx, qa in enumerate(qna, start=1):
            combined_text += f"""
Q{idx}: {qa['question']}
A{idx}: {qa['answer']}
"""

        prompt = f"""
You are a senior technical interview evaluator.

Your task is to evaluate the candidate ONLY based on the provided interview content.
Be fair, supportive, and slightly favorable to the candidate when assigning scores.
Do NOT be overly strict or harsh.

=====================
EVALUATION RULES
=====================

1. Evaluation Scope:
   - Evaluate ONLY what the candidate has actually answered.
   - Do NOT assume missing knowledge or penalize for things not asked.
   - Do NOT compare the candidate against an ideal or senior-level benchmark unless clearly demonstrated.

2. Missing or Empty Answers:
   - If the candidate did NOT provide an answer, provided silence, or said "I don't know":
     • Assign a score of 0 for that portion.
     • Do NOT use words like "undefined", "null", or similar.

3. Scoring Guidelines (0–10):
   - 0–2 → No answer or completely incorrect
   - 3–4 → Very basic or partially relevant understanding
   - 5–8 → Acceptable, basic understanding with minor gaps
   - 9 -10 → Good, clear, and mostly correct explanation
   

   When in doubt, lean toward the higher reasonable score.

4. Score Definitions:
   - technical_score:
     • Understanding of concepts mentioned in the interview
     • Correctness and relevance of technical responses
   - communication_score:
     • Clarity of explanation
     • Ability to express thoughts understandably
     • Logical flow (even if technically basic)
   - overall_score:
     • Average of technical_score and communication_score
     • Rounded to the nearest integer

5. Recommendation Logic:
   - "Yes" → overall_score >= 7
   - "Maybe" → overall_score between 4 to 6
   - "No" → overall_score < 4

6. Feedback Guidelines:
   - Keep feedback brief, constructive, and encouraging.
   - Highlight strengths first, then gently mention improvement areas.
   - Do NOT use harsh, discouraging, or negative language.

=====================
INTERVIEW CONTENT
=====================
{combined_text}

=====================
OUTPUT FORMAT
=====================

STRICT RULES:
- Return ONLY valid JSON
- No markdown
- No extra text
- All scores MUST be integers between 0 and 10
- Recommendation MUST be exactly one of: "Yes", "Maybe", "No"

Return this JSON format exactly:
{{
  "technical_score": 0,
  "communication_score": 0,
  "overall_score": 0,
  "recommendation": "Yes",
  "feedback": "Brief evaluation"
}}
"""

        openai_client = get_openai_client()
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a strict evaluator. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2
        )
        
        result_text = response.choices[0].message.content.strip()
        
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0].strip()
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0].strip()
        
        result = json.loads(result_text)
        
        scheduled_interviews.update_one(
            {"interview_id": interview_id},
            {
                "$set": {
                    "evaluation": result,
                    "completed_at": datetime.utcnow(),
                    "interview_status": "completed"
                }
            }
        )

        print(f"✅ Interview {interview_id} evaluated and saved")
        return jsonify(result), 200
    
    except Exception as e:
        print(f"Evaluation error: {e}")
        return jsonify({"error": str(e)}), 500


@interviews_bp.route('/upload-video/<interview_id>', methods=['POST'])
def upload_video(interview_id):
    """Upload interview video to Google Drive"""
    try:
        if 'video' not in request.files:
            return jsonify({"error": "No video file provided"}), 400
        
        video_file = request.files['video']
        candidate_name = request.form.get('candidate_name', 'Candidate')
        candidate_email = request.form.get('candidate_email', '')

        if video_file.filename == '':
            return jsonify({"error": "No selected file"}), 400
        
        file_content = video_file.read()
        filename = f"Interview_{candidate_name}_{interview_id}.webm"
        
        result = upload_to_drive(file_content, filename)

        from services.mongodb_service import scheduled_interviews

        scheduled_interviews.update_one(
            {"interview_id": interview_id},
            {
                "$set": {
                    "interview_status": "completed",
                    "completed_at": datetime.utcnow()
                }
            }
        )

        if result and result.get('id'):
            print(f"✅ Video uploaded to Google Drive: {result.get('id')}")
            return jsonify({
                "status": "success",
                "message": "Video uploaded successfully",
                "file_id": result.get('id'),
                "file_link": result.get('link')
            }), 200
        else:
            return jsonify({
                "status": "error",
                "message": "Failed to upload video to Google Drive"
            }), 500
    
    except Exception as e:
        print(f"Error uploading video: {e}")
        return jsonify({"error": str(e)}), 500


def parse_questions(raw_text):
    """Parse raw text into questions list"""
    raw_questions = raw_text.split("\n")
    questions = []
    
    for q in raw_questions:
        q = q.strip()
        if not q:
            continue
        
        for i in range(len(q)):
            if q[i].isdigit():
                continue
            if q[i] in '.):- ':
                q = q[i+1:].strip()
                break
        
        if q:
            questions.append(q)
    
    return questions[:5]