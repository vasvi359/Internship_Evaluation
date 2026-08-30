# agents.py

def analyzer_agent(query, llm):
    return llm.invoke(f"""
    You are the EduMind Analyzer Agent.
    
    Analyze this student query and identify:
    - Key concepts being asked
    - Topic area it belongs to
    - Depth of answer required (basic/intermediate/advanced)
    
    Query: {query}
    
    Return a precise 2-line analysis only.
    """).content


def structure_and_polish_agent(answer, llm, wants_long=False):
    min_words = "600 words minimum" if wants_long else "400 words minimum"
    return llm.invoke(f"""
    You are the EduMind Structure & Polish Agent.

    Your job is to take the content below and make it COMPREHENSIVE and DETAILED.

    STRICT RULES:
    - Make the answer as LONG and DETAILED as possible
    - Add more explanation, examples, and elaboration wherever possible
    - Use proper headings (##), sub-headings, bullet points, and bold text
    - Preserve ALL existing facts — never remove or condense anything
    - Add relevant real-world examples to explain each concept
    - Each section must be thoroughly explained with depth
    - {min_words} in your final response
    - If a concept has sub-parts, explain each sub-part separately
    - End with a Summary section recapping key points

    Content to expand and polish:
    {answer}
    """).content


def summarizer_agent(answer, llm):
    return llm.invoke(f"""
    You are the EduMind Summarizer Agent.
    
    Condense the following content into a clear, structured, and short summary.
    Use bullet points for key concepts. Focus strictly on brevity.
    Keep it student-friendly and exam-focused.
    
    Content: {answer}
    """).content


def feedback_agent(user_answers, questions, context, llm):
    return llm.invoke(f"""
    You are the EduMind Evaluator Agent.
    
    Evaluate the student's answers against the source material.
    
    Source Material:
    {context}
    
    Questions Asked:
    {questions}
    
    Student's Answers:
    {user_answers}
    
    Provide a structured Report Card:
    
    Overall Score: X/100
    
    Question-by-Question Review:
    - For each answer: what was correct, what was missing
    
    Strengths: What the student did well
    
    Areas to Improve: Topics that need more practice
    
    Study Tip: One specific actionable tip for next session
    
    Be fair, specific, and encouraging. No emojis.
    """).content


def question_generator_agent(context, llm, history=[], count=5):
    # Get previous questions from memory to avoid repetition
    past_questions = ""
    for m in history:
        if isinstance(m, dict):
            content = m.get("content", "")
            if isinstance(content, str) and len(content) > 50:
                past_questions += content[:300] + "\n"

    # Build strict avoid instruction
    avoid_instruction = ""
    if past_questions.strip():
        avoid_instruction = f"""
STRICT RULE — PREVIOUSLY ASKED QUESTIONS (DO NOT USE ANY OF THESE):
{past_questions[:800]}

The above questions have ALREADY been asked. You are STRICTLY FORBIDDEN
from repeating, rephrasing, or using similar versions of any of the above.
Generate COMPLETELY DIFFERENT questions on different aspects of the topic.
"""
    else:
        avoid_instruction = "No previous questions exist. Generate fresh questions."

    return llm.invoke(f"""
    You are the EduMind Question Generator Agent.

    {avoid_instruction}

    Now generate exactly {count} BRAND NEW exam questions based ONLY on the
    core concepts in the study material below.

    Rules:
    - Focus on KEY CONCEPTS only
    - Ignore metadata like course codes, batch years, URLs, book titles
    - Each question must test deep understanding — not memorization
    - Keep each question clear and under 2 lines
    - Number them 1 to {count}
    - Mix question types: explain, compare, analyze, apply
    - Every single question must be COMPLETELY DIFFERENT from the forbidden list above

    Study Material:
    {context}

    Generate {count} COMPLETELY NEW concept-based exam questions now:
    """).content