# backend.py

import re
import logging
import prompts
from agents import analyzer_agent, summarizer_agent, feedback_agent, question_generator_agent, structure_and_polish_agent
from rag import retrieve_context

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("EduMindBackend")


class EduMindMemory:
    def __init__(self):
        self.current_topic = None
        self.recent_topics = []
        self.last_questions = None
        self.last_context = None
        self.last_answer = None
        self.quiz_active = False
        self.session_history = []
        self.session_summary = ""
        self.quiz_topic = None
        self.quiz_count = 0
        self.quiz_answers_given = False
        self.topic_scores = {}
        self.total_questions_answered = 0
        self.total_quizzes_taken = 0
        self.answer_cache = {}
        self.context_cache = {}

    def update(self, topic=None, questions=None, context=None):
        if topic:
            self.current_topic = topic
            if topic not in self.recent_topics:
                self.recent_topics.append(topic)
                if len(self.recent_topics) > 5:
                    self.recent_topics.pop(0)
        if questions:
            self.last_questions = questions
            self.quiz_active = True
        if context:
            self.last_context = context

    def add_to_history(self, role, content):
        self.session_history.append({
            "role": role,
            "content": str(content)[:2000]
        })
        if len(self.session_history) > 10:
            self.session_history = self.session_history[-10:]

    def get_recent_history(self, n=4):
        recent = self.session_history[-n:]
        return "\n".join([
            f"{m['role'].upper()}: {m['content']}"
            for m in recent
        ])

    def get_status(self):
        analytics = {}
        for t, scores in self.topic_scores.items():
            if scores:
                analytics[t] = {
                    "scores": scores,
                    "average_score": sum(scores) / len(scores),
                    "best_score": max(scores),
                    "improvement_rate": (scores[-1] - scores[0]) if len(scores) > 1 else 0
                }
        return {
            "topic": self.current_topic,
            "recent_topics": self.recent_topics,
            "quiz_active": self.quiz_active,
            "history_length": len(self.session_history),
            "quiz_topic": self.quiz_topic,
            "quiz_count": self.quiz_count,
            "quiz_answers_given": self.quiz_answers_given,
            "analytics_dashboard": analytics,
            "total_questions_answered": self.total_questions_answered,
            "total_quizzes_taken": self.total_quizzes_taken,
            "long_term_summary": bool(self.session_summary)
        }


class EduMindOrchestrator:
    def __init__(self, memory: EduMindMemory):
        self.memory = memory

    def detect_intent_rules(self, query):
        query_lower = query.lower().strip()

        # General chat
        if len(query.split()) <= 5 and any(word in query_lower for word in [
            "hi", "hii", "hello", "hey", "thanks", "thank you",
            "ok", "okay", "bye", "good", "great", "nice", "cool"
        ]):
            return "GENERAL_CHAT"

        # Quiz adjustment check
        if self.memory.quiz_active:
            words_indicative_of_adjustment = ["only", "fewer", "instead", "too many", "reduce", "increase", "regenerate"]
            number_words = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
            has_adjustment_word = any(word in query_lower for word in words_indicative_of_adjustment)
            has_number = any(re.search(rf'\b{word}\b', query_lower) for word in number_words) or bool(re.search(r'\b\d+\b', query_lower))

            if not (has_adjustment_word and has_number):
                if re.search(r'\b(1[\.\)]|2[\.\)]|3[\.\)])', query):
                    return "EVALUATE"
                has_explicit_submit = any(word in query_lower for word in ["ans:", "my answer to", "here is my", "score me", "evaluate my"])
                if has_explicit_submit:
                    return "EVALUATE"

        # Evaluation intent
        if any(word in query_lower for word in [
            "my answer", "grade", "evaluate", "score me",
            "check my", "grade my", "here is my answer",
            "my response", "assess", "am i right", "am i wrong",
            "is it correct", "feedback", "how did i do", "score this"
        ]):
            return "EVALUATE"

        # ── QUIZ intent ── checked BEFORE followup
        # Handles: "quiz", "questions", "quiz related to this", "questions on this" etc.
        wants_quiz = any(word in query_lower for word in [
            "question", "quiz", "exam", "generate questions",
            "important questions", "prepare", "test me", "practice",
            "give me questions", "ask me something", "start quiz",
            "quiz related", "questions related", "quiz on this",
            "questions on this", "quiz about", "test on this",
            "quiz about this", "questions about this"
        ])

        has_answer_request = any(phrase in query_lower for phrase in [
            "give answers", "provide answers", "answer those",
            "answers of", "what are the answers", "their answers",
            "with answers", "and answers", "with solution"
        ])

        if wants_quiz and has_answer_request:
            return "QUIZ_WITH_ANSWERS"
        if wants_quiz:
            return "QUIZ"

        # Explain intent
        if any(word in query_lower for word in [
            "explain", "what is", "define", "describe",
            "tell me about", "summarize", "overview",
            "how does", "why is", "what are"
        ]):
            return "EXPLAIN"

        # Followup intent
        if any(phrase in query_lower for phrase in [
            "those", "these", "previous", "above",
            "elaborate", "continue", "next", "go on",
            "more about", "additionally", "furthermore"
        ]):
            return "FOLLOWUP"

        return "UNKNOWN"

    def detect_intent_llm(self, query, llm):
        try:
            llm_decision = llm.invoke(prompts.INTENT_CLASSIFICATION_PROMPT.format(query=query)).content.strip()
            if llm_decision in ["GENERAL_CHAT", "EXPLAIN", "QUIZ", "EVALUATE", "FOLLOWUP"]:
                return llm_decision
        except Exception as e:
            logger.error(f"Fallback intent call failed: {e}")
        return "GENERAL"

    def should_skip_analyzer(self, query, intent):
        if intent in ["EVALUATE", "GENERAL_CHAT"]:
            return True
        if len(query.split()) < 5:
            return True
        return False

    def should_use_polish(self, intent):
        return intent in ["EXPLAIN", "FOLLOWUP", "GENERAL"]

    def route(self, query, context, chunks, llm, status_callback=None):
        def report(agent, status="active"):
            if status_callback:
                status_callback(agent, status)

        intent = self.detect_intent_rules(query)
        if intent == "UNKNOWN":
            intent = self.detect_intent_llm(query, llm)

        agents_used = []
        wants_long = any(word in query.lower() for word in [
            "long", "detailed", "in detail", "elaborate", "comprehensive",
            "thorough", "explain fully", "in depth", "complete explanation"
        ])

        cache_key = f"{intent}_{query.strip().lower()}"
        if cache_key in self.memory.answer_cache:
            return self.memory.answer_cache[cache_key]

        result = {
            "answer": "",
            "evaluation": "",
            "questions": "",
            "sources": [],
            "agents_used": [],
            "intent": intent,
            "memory_status": self.memory.get_status()
        }

        # Long-term memory compression
        if len(self.memory.session_history) >= 12:
            try:
                report("Summarizer Agent", "active")
                history_block = self.memory.get_recent_history(10)
                self.memory.session_summary = summarizer_agent(
                    f"Update/Extend the running log summary using this history chain.\nSummary: {self.memory.session_summary}\nHistory:\n{history_block}",
                    llm
                )
                self.memory.session_history = self.memory.session_history[-2:]
            except Exception as e:
                logger.error(f"Long-term memory compiler error: {e}")

        if intent not in ["GENERAL_CHAT"] and chunks:
            result["sources"] = [c.metadata.get("source", f"Chunk {i+1}") for i, c in enumerate(chunks)]

        # ── ROUTE 0: GENERAL CHAT ──
        if intent == "GENERAL_CHAT":
            report("Analyzer Agent", "active")
            try:
                answer = llm.invoke(prompts.GENERAL_CHAT_PROMPT.format(query=query)).content
            except Exception as e:
                answer = f"Hello! I am ready to assist you. ({e})"
            self.memory.add_to_history("user", query)
            self.memory.add_to_history("assistant", answer)
            result["answer"] = answer
            result["agents_used"] = ["Analyzer Agent"]
            return result

        # ── ROUTE 1: EVALUATE ──
        if intent == "EVALUATE":
            report("Evaluator Agent", "active")
            agents_used.append("Evaluator Agent")
            last_questions = self.memory.last_questions or "No active questions found in memory."
            try:
                evaluation = feedback_agent(query, last_questions, context, llm)
                score_match = re.search(r'(\d+)\s*%', evaluation) or re.search(r'score:\s*(\d+)/', evaluation.lower())
                if score_match and self.memory.current_topic:
                    extracted_score = int(score_match.group(1))
                    if self.memory.current_topic not in self.memory.topic_scores:
                        self.memory.topic_scores[self.memory.current_topic] = []
                    self.memory.topic_scores[self.memory.current_topic].append(extracted_score)
                self.memory.total_questions_answered += self.memory.quiz_count
            except Exception as e:
                evaluation = f"Evaluation failed: {e}"
            self.memory.quiz_active = False
            self.memory.add_to_history("user", query)
            self.memory.add_to_history("assistant", evaluation)
            result["evaluation"] = evaluation
            result["agents_used"] = agents_used
            return result

        # ── ROUTE 2: QUIZ ──
        if intent in ["QUIZ", "QUIZ_WITH_ANSWERS"]:
            has_answer_request = intent == "QUIZ_WITH_ANSWERS"

            difficulty = "medium"
            for level in ["easy", "medium", "hard", "interview", "advanced"]:
                if level in query.lower():
                    difficulty = level
                    break

            blooms_level = "general application"
            for level in ["remember", "understand", "apply", "analyze", "evaluate", "create"]:
                if level in query.lower():
                    blooms_level = level
                    break

            question_type = "Conceptual/Theoretical"
            if any(w in query.lower() for w in ["mcq", "multiple choice", "objective"]):
                question_type = "MCQ (Multiple Choice Questions with options A, B, C, D)"
            elif "short answer" in query.lower() or "5 marks" in query.lower():
                question_type = "Short Answer Questions (5 Marks format)"
            elif "long answer" in query.lower() or "10 marks" in query.lower():
                question_type = "Long Form Comprehensive Questions (10 Marks format)"

            if self.memory.current_topic:
                enriched_context = f"Topic: {self.memory.current_topic}\n\n{context}"
            else:
                enriched_context = context
                try:
                    topic = llm.invoke(f"Extract only the study topic from: '{query}'. Return only the raw topic name.").content.strip()
                    self.memory.update(topic=topic)
                except Exception:
                    self.memory.update(topic=query)

            if not self.should_skip_analyzer(query, intent):
                try:
                    report("Analyzer Agent", "active")
                    agents_used.append("Analyzer Agent")
                    analyzer_agent(query, llm)
                except Exception as e:
                    logger.error(f"Analyzer skipped: {e}")

            count = 5
            words_to_nums = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
            count_match = re.search(r'(\d+)', query)
            if count_match:
                count = int(count_match.group(1))
            else:
                for word, num in words_to_nums.items():
                    if re.search(rf'\b{word}\b', query.lower()):
                        count = num
                        break

            report("Question Generator", "active")
            agents_used.append("Question Generator")

            try:
                generation_context = f"Generate {count} questions of type: {question_type}. Cognitive depth: {blooms_level}. Difficulty: {difficulty}.\n\n{enriched_context}"
                questions = question_generator_agent(generation_context, llm, history=self.memory.session_history, count=count)
            except Exception as e:
                questions = f"Question generation failed: {e}"
                result["questions"] = questions
                return result

            self.memory.update(questions=questions, context=enriched_context)
            self.memory.quiz_topic = self.memory.current_topic
            self.memory.quiz_count = count
            self.memory.total_quizzes_taken += 1

            if not has_answer_request:
                self.memory.quiz_answers_given = False
                self.memory.add_to_history("user", query)
                self.memory.add_to_history("assistant", questions)
                result["questions"] = questions.strip()
                result["agents_used"] = agents_used
                return result

            # Quiz with answers
            report("Structure & Polish Agent", "active")
            agents_used.append("Structure & Polish Agent")
            self.memory.quiz_answers_given = True
            try:
                qa_answer = llm.invoke(prompts.QUESTIONS_RESOLUTION_PROMPT.format(
                    count=count, questions=questions, enriched_context=enriched_context
                )).content
            except Exception as e:
                qa_answer = f"Answer generation failed: {e}"

            self.memory.add_to_history("user", query)
            self.memory.add_to_history("assistant", qa_answer)
            self.memory.quiz_active = False
            self.memory.last_answer = qa_answer
            result["answer"] = qa_answer.strip()
            result["agents_used"] = agents_used
            self.memory.answer_cache[cache_key] = result
            return result

        # ── ROUTE 3: EXPLAIN ──
        if intent == "EXPLAIN":
            if not self.should_skip_analyzer(query, intent):
                try:
                    report("Analyzer Agent", "active")
                    agents_used.append("Analyzer Agent")
                    analyzer_agent(query, llm)
                except Exception as e:
                    logger.error(f"Analyzer failed: {e}")

            try:
                topic = llm.invoke(f"Extract only the study topic from: '{query}'. Return only the raw topic name.").content.strip()
                self.memory.update(topic=topic, context=context)
            except Exception:
                self.memory.update(topic=query, context=context)

            memory_history = self.memory.get_recent_history(4)
            detail_instruction = "Provide a DETAILED, thorough, and comprehensive answer." if wants_long else "Provide a clear, structured and detailed answer."

            try:
                answer = llm.invoke(prompts.EXPLAIN_PROMPT.format(
                    session_summary=self.memory.session_summary,
                    memory_history=memory_history,
                    context=context,
                    query=query,
                    detail_instruction=detail_instruction
                )).content
            except Exception as e:
                answer = f"LLM error: {e}"

            summarize_keywords = ["summar", "short", "tldr", "condense", "brief", "summary", "gist"]
            try:
                if any(k in query.lower() for k in summarize_keywords):
                    report("Summarizer Agent", "active")
                    agents_used.append("Summarizer Agent")
                    final_answer = summarizer_agent(answer, llm)
                else:
                    report("Structure & Polish Agent", "active")
                    agents_used.append("Structure & Polish Agent")
                    final_answer = structure_and_polish_agent(answer, llm, wants_long=wants_long)
            except Exception as e:
                logger.error(f"Post-processing error: {e}")
                final_answer = answer

            self.memory.add_to_history("user", query)
            self.memory.add_to_history("assistant", final_answer)
            self.memory.last_answer = final_answer
            result["answer"] = final_answer.strip()
            result["agents_used"] = agents_used
            self.memory.answer_cache[cache_key] = result
            return result

        # ── ROUTE 3.5: FOLLOWUP ──
        if intent == "FOLLOWUP":
            report("Analyzer Agent", "active")
            agents_used.append("Analyzer Agent")

            memory_history = self.memory.get_recent_history(6)
            last_questions = self.memory.last_questions or ""
            last_context = self.memory.last_context or context
            historical_precise_answer = self.memory.last_answer or "No previous answer cached."
            detail_instruction = "Answer in GREAT DETAIL." if wants_long else "Answer naturally, completely and in detail."

            try:
                answer = llm.invoke(prompts.FOLLOWUP_PROMPT.format(
                    session_summary=self.memory.session_summary,
                    historical_precise_answer=historical_precise_answer,
                    memory_history=memory_history,
                    last_questions=last_questions,
                    last_context=last_context,
                    query=query,
                    detail_instruction=detail_instruction
                )).content
            except Exception as e:
                answer = f"Followup processing error: {e}"

            summarize_keywords = ["summar", "short", "tldr", "condense", "brief", "summary", "gist"]
            try:
                if any(k in query.lower() for k in summarize_keywords):
                    report("Summarizer Agent", "active")
                    agents_used.append("Summarizer Agent")
                    final_answer = summarizer_agent(answer, llm)
                else:
                    report("Structure & Polish Agent", "active")
                    agents_used.append("Structure & Polish Agent")
                    final_answer = structure_and_polish_agent(answer, llm, wants_long=wants_long)
            except Exception as e:
                logger.error(f"Followup post-processing error: {e}")
                final_answer = answer

            self.memory.add_to_history("user", query)
            self.memory.add_to_history("assistant", final_answer)
            self.memory.last_answer = final_answer
            result["answer"] = final_answer.strip()
            result["agents_used"] = agents_used
            return result

        # ── ROUTE 4: GENERAL ──
        if not self.should_skip_analyzer(query, intent):
            try:
                report("Analyzer Agent", "active")
                agents_used.append("Analyzer Agent")
                analyzer_agent(query, llm)
            except Exception as e:
                logger.error(f"Analyzer failed: {e}")

        memory_history = self.memory.get_recent_history(4)
        detail_instruction = "Answer in GREAT DETAIL." if wants_long else "Answer clearly and in detail."

        try:
            answer = llm.invoke(prompts.GENERAL_PROMPT.format(
                session_summary=self.memory.session_summary,
                memory_history=memory_history,
                context=context,
                query=query,
                detail_instruction=detail_instruction
            )).content
        except Exception as e:
            answer = f"General processing error: {e}"

        if self.should_use_polish(intent):
            summarize_keywords = ["summar", "short", "tldr", "condense", "brief", "summary", "gist"]
            try:
                if any(k in query.lower() for k in summarize_keywords):
                    report("Summarizer Agent", "active")
                    agents_used.append("Summarizer Agent")
                    answer = summarizer_agent(answer, llm)
                else:
                    report("Structure & Polish Agent", "active")
                    agents_used.append("Structure & Polish Agent")
                    answer = structure_and_polish_agent(answer, llm, wants_long=wants_long)
            except Exception as e:
                logger.error(f"Final formatting error: {e}")

        self.memory.add_to_history("user", query)
        self.memory.add_to_history("assistant", answer)
        self.memory.last_answer = answer
        result["answer"] = answer.strip()
        result["agents_used"] = agents_used
        self.memory.answer_cache[cache_key] = result
        return result


def process_query(query, vectorstore, memory, llm, status_callback=None):
    query_lower = query.lower().strip()
    is_general_chat = len(query.split()) <= 5 and any(word in query_lower for word in [
        "hi", "hii", "hello", "hey", "thanks", "thank you",
        "ok", "okay", "bye", "good", "great", "nice", "cool"
    ])

    if is_general_chat:
        orchestrator = EduMindOrchestrator(memory)
        return orchestrator.route(query, "", [], llm, status_callback=status_callback)

    if vectorstore is None:
        return {
            "answer": "Please upload your study notes from the sidebar first!",
            "evaluation": "", "questions": "", "sources": [],
            "agents_used": [], "intent": "NONE", "memory_status": {}
        }

    context_cache_key = query_lower
    chunks = []

    if context_cache_key in memory.context_cache:
        context, chunks = memory.context_cache[context_cache_key]
    else:
        try:
            docs_with_scores = vectorstore.similarity_search_with_relevance_scores(query, k=4)
            valid_docs = []
            for doc, score in docs_with_scores:
                if score >= 0.20:  # Lowered threshold from 0.35 to 0.20
                    valid_docs.append(doc)
                    chunks.append(doc)

            if not valid_docs:
                # Fallback — use all top results even below threshold
                chunks = vectorstore.similarity_search(query, k=4)
                context = "\n\n".join([doc.page_content for doc in chunks])
            else:
                context = "\n\n".join([doc.page_content for doc in valid_docs])

            memory.context_cache[context_cache_key] = (context, chunks)

        except Exception as e:
            logger.error(f"RAG retrieval error: {e}")
            try:
                chunks = vectorstore.similarity_search(query, k=4)
                context = "\n\n".join([doc.page_content for doc in chunks])
            except Exception as err:
                logger.critical(f"Fatal retrieval error: {err}")
                context = ""

    orchestrator = EduMindOrchestrator(memory)
    return orchestrator.route(query, context, chunks, llm, status_callback=status_callback)