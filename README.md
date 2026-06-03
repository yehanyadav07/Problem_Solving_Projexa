# Problem_Solving_Projexa
Smart E-Waste
Smart E-Waste is an AI-powered platform for identifying electronic waste and locating nearby recycling centers. By scanning your electronic items, the system tells you exactly what they are, why they should be recycled, and where you can drop them off or schedule a pickup.

⚠️ Important: API Keys Required
This application relies on external AI services to accurately classify e-waste images. To use this app fully, you MUST provide your own API keys.

Without these keys, the app uses a fallback list of specific items and deterministic logic (which will only guess the item based on the filename or run a random selection). This is only meant for basic demonstration purposes.

For real dynamic AI functionality, you need:

1. Gemini API Key: Used for detailed image analysis, classification, and generating environmental impact descriptions.
2. Google Vision API Key: Used for initial image context, label detection, and logo recognition to improve accuracy.
