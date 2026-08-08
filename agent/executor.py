import threading
from typing import Callable

from agent.planner       import create_plan, replan
from agent.error_handler import analyze_error, ErrorDecision
from core.llm            import get_gateway


def _inject_context(params: dict, tool: str, step_results: dict, goal: str = "") -> dict:
    """Last-resort content injection for a write step that has nothing to write.

    This used to be the *only* way one step's output reached another: it
    joined every previous result over 100 characters and dropped the lot into
    `content`, for one tool and one parameter. Plans now say what they mean —
    `"content": "{{step:1}}"` — and `Plan.resolve` fills it in.

    It survives as a fallback for the case the placeholder cannot cover: a
    planner that produced a write step with no content and no reference at
    all. It also carries the translation pass, which has nothing to do with
    data flow but was tangled up in here and would be silently lost if this
    went away.
    """
    if not step_results:
        return params

    params = dict(params)

    if tool == "file_controller" and params.get("action") in ("write", "create_file"):
        content = params.get("content", "")
        if not content or len(content) < 50:
            all_results = [
                v for v in step_results.values()
                if v and len(v) > 100 and v not in ("Done.", "Completed.")
            ]
            if all_results:
                combined = "\n\n---\n\n".join(all_results)
                translated = _translate_to_goal_language(combined, goal)
                params["content"] = translated
                print("[Executor] 💉 Injected + translated content (no reference given)")

    return params
def _detect_language(text: str) -> str:
    try:
        response = get_gateway().chat(
            f"What language is this text written in? "
            f"Reply with ONLY the language name in English (e.g. Turkish, English, French).\n\n"
            f"Text: {text[:200]}",
            model="gemini-2.5-flash-lite", max_tokens=20, temperature=0.0,
        )
        return response.text.strip()
    except Exception:
        return "English"


def _translate_to_goal_language(content: str, goal: str) -> str:
    if not goal:
        return content
    try:
        target_lang = _detect_language(goal)
        print(f"[Executor] 🌐 Translating to: {target_lang}")

        prompt = (
            f"You are a professional translator. "
            f"Translate the following text into {target_lang}.\n"
            f"IMPORTANT:\n"
            f"- Translate EVERYTHING, leave nothing in English\n"
            f"- Keep all facts, numbers, and data intact\n"
            f"- Keep the structure and formatting\n"
            f"- Output ONLY the translated text, nothing else\n\n"
            f"Text to translate:\n{content[:4000]}"
        )
        translated = get_gateway().chat(prompt, temperature=0.2).text.strip()
        print(f"[Executor] ✅ Translation done ({target_lang})")
        return translated
    except Exception as e:
        print(f"[Executor] ⚠️ Translation failed: {e}")
        return content

def _call_tool(tool: str, parameters: dict, speak: Callable | None) -> str:
    """Dispatch through the shared tool registry (headless: no UI player)."""
    from core.tools import EMPTY_RESULT_FALLBACKS, get_registry

    result = get_registry().dispatch(tool, parameters, player=None, speak=speak)
    return result or EMPTY_RESULT_FALLBACKS.get(tool, "Done.")

class AgentExecutor:

    MAX_REPLAN_ATTEMPTS = 2

    def execute(
        self,
        goal:        str,
        speak:       Callable | None        = None,
        cancel_flag: threading.Event | None = None,
    ) -> str:
        print(f"\n[Executor] 🎯 Goal: {goal}")

        replan_attempts = 0
        completed_steps = []
        step_results    = {} 
        plan            = create_plan(goal)

        while True:
            steps = list(plan.steps)

            if not steps:
                msg = "I couldn't create a valid plan for this task."
                if speak: speak(msg)
                return msg

            success      = True
            failed_step  = None
            failed_error = ""

            for step in steps:
                if cancel_flag and cancel_flag.is_set():
                    if speak: speak("Task cancelled.")
                    return "Task cancelled."

                step_num = step.step
                tool     = step.tool
                desc     = step.description

                # Real data flow: {{step:N}} becomes step N's actual output.
                params = plan.resolve(step, step_results)
                params = _inject_context(params, tool, step_results, goal=goal)

                used = sorted(step.references())
                using = f" (using step {', '.join(map(str, used))})" if used else ""
                print(f"\n[Executor] ▶️ Step {step_num}: [{tool}] {desc}{using}")

                attempt = 1
                step_ok = False

                while attempt <= 3:
                    if cancel_flag and cancel_flag.is_set():
                        break
                    try:
                        result = _call_tool(tool, params, speak)
                        step_results[step_num] = result 
                        completed_steps.append(step)
                        print(f"[Executor] ✅ Step {step_num} done: {str(result)[:100]}")
                        step_ok = True
                        break

                    except Exception as e:
                        error_msg = str(e)
                        print(f"[Executor] ❌ Step {step_num} attempt {attempt} failed: {error_msg}")

                        recovery = analyze_error(step, error_msg, attempt=attempt)
                        decision = recovery["decision"]
                        user_msg = recovery.get("user_message", "")

                        if speak and user_msg:
                            speak(user_msg)

                        if decision == ErrorDecision.RETRY:
                            attempt += 1
                            import time; time.sleep(2)
                            continue

                        elif decision == ErrorDecision.SKIP:
                            print(f"[Executor] ⏭️ Skipping step {step_num}")
                            completed_steps.append(step)
                            step_ok = True
                            break

                        elif decision == ErrorDecision.ABORT:
                            msg = f"Task aborted. {recovery.get('reason', '')}"
                            if speak: speak(msg)
                            return msg

                        else:
                            # REPLAN — hand the failure to the planner, which
                            # builds a revised plan from real registry tools.
                            failed_step  = step
                            failed_error = error_msg
                            success      = False
                            break

                if not step_ok and not failed_step:
                    failed_step  = step
                    failed_error = "Max retries exceeded"
                    success      = False

                if not success:
                    break

            if success:
                return self._summarize(goal, completed_steps, speak)

            if replan_attempts >= self.MAX_REPLAN_ATTEMPTS:
                msg = f"Task failed after {replan_attempts} replan attempts."
                if speak: speak(msg)
                return msg

            if speak: speak("Adjusting my approach.")

            replan_attempts += 1
            plan = replan(goal, completed_steps, failed_step, failed_error)

    def _summarize(self, goal: str, completed_steps: list, speak: Callable | None) -> str:
        fallback = f"All done. Completed {len(completed_steps)} steps for: {goal[:60]}."
        try:
            steps_str = "\n".join(f"- {s.description}" for s in completed_steps)
            prompt    = (
                f'User goal: "{goal}"\n'
                f"Completed steps:\n{steps_str}\n\n"
                "Write a single natural sentence summarizing what was accomplished. "
                "Be direct and positive."
            )
            summary = get_gateway().chat(
                prompt, model="gemini-2.5-flash-lite", max_tokens=100, temperature=0.5,
            ).text.strip()
            if speak: speak(summary)
            return summary
        except Exception:
            if speak: speak(fallback)
            return fallback