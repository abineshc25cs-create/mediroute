# MediRoute AI V9.3

V9.3 keeps live GPS as the default for public and ambulance users. Fixed pickup points are not used for normal public hospital discovery; the Nagercoil location is an explicitly labelled judge-only ambulance demo.

## Emergency blood-contact network
Public users register their name, phone and self-reported blood group. They can explicitly allow authorized hospital staff to contact them for an urgent blood-group request. A logged-in hospital can open **🩸 Blood**, choose a required blood group, and see matching opted-in public contacts with their name and phone number. The hospital can call the person to verify identity, current availability and blood group before any clinical use.

This feature is an additional contact network, not a blood-stock system and not proof that a person is an eligible donor. It deliberately does not require hospitals to maintain blood-unit counts inside MediRoute.

For production deployment, the hospital-side contact directory must use authenticated role-based access, consent records, audit logging, rate limits, abuse prevention and applicable privacy/data-protection controls. The hackathon build demonstrates the complete workflow with in-memory data.


## Natural-language emergency NLP (V9.4)

The ambulance symptom field is intentionally free-form. MediRoute no longer rejects an emergency just because a phrase is not in a fixed keyword list.

### Optional production integration
Set these server-side environment variables to enable Azure AI Language Text Analytics for health:

- `AZURE_LANGUAGE_ENDPOINT=https://<resource>.cognitiveservices.azure.com`
- `AZURE_LANGUAGE_KEY=<server-side-secret>`
- `AZURE_LANGUAGE_API_VERSION=2026-05-01` (optional; can be changed when Azure changes the API version)

MediRoute sends the raw emergency description to the provider and uses returned medical entities as additional evidence. Azure documents healthcare NER, relation extraction, entity linking and assertion detection for unstructured medical text. If the key/endpoint is absent or the provider is unavailable, MediRoute automatically uses its local broad emergency classifier instead of rejecting the request.

The API key is never placed in the browser. For production, use an appropriate healthcare/privacy review, access control, retention policy and authorized cloud configuration before sending real patient information to an external service.

The NLP result is decision-support only; it is not a diagnosis.


## Deploy + Android APK (PWA)
1. Push this folder to GitHub. In Render: New > Blueprint (uses render.yaml) or New > Web Service with
   build `pip install -r requirements.txt` and start `uvicorn main:app --host 0.0.0.0 --port $PORT`.
2. Add Twilio/Azure keys only in Render > Environment (never in git).
3. Open the https URL on your phone in Chrome: menu > Install app.
4. For an APK: pwabuilder.com > enter your URL > Package for stores > Android. Put the generated
   assetlinks.json into static/.well-known/ and redeploy to hide the address bar.
Note: data is in memory and resets when the server restarts or the free Render instance sleeps.
