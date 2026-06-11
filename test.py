import google.generativeai as genai

API_KEY = "AQ.Ab8RN6K7i0xeRMb1VehgfdO50uX8HG46P88bzjUNvBju0sOT98-dDw"

genai.configure(api_key=API_KEY)

model = genai.GenerativeModel("gemini-2.0-flash")

response = model.generate_content("hello")

print(response.text)