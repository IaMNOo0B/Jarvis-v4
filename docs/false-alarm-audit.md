# Step 01 Saboot — Proactivity False-Alarm Audit

**Frame Step 01 (Math Foundations) ka saboot document.** Usko khada hai
`jarvis/measure.py` pe — `posterior()` aur `false_alarm_audit()` — aur wahi
numbers yahan likhe hain jo code deta hai (tests: `tests/test_measure.py`).

## Ek line me sabak

Koi detector "99% sahi" hone ke bawjud aksar jhooti ghanti bajata hai, jab
woh cheez dhoondh raha hai woh **nayaab (rare)** ho. Wajah base rate hai:

> posterior = sens × prevalence / (sens × prevalence + (1 − spec) × (1 − prevalence))

Textbook misaal (Phase 01, Lesson 07): koi bimari 10,000 me se 1 ko hai
(prevalence 0.0001), test 99% sensitive aur 99% specific hai. Phir bhi
positive aane par sach me bimar hone ka chance sirf **0.98%** hai — matlab
**har 100 ghantiyon me ~99 jhooti**. Detector kharab nahi hai; ghanti
bajana hi galat faisla hai.

Isi liye JARVIS ki har proactive ghanti ko pehle is hisaab se guzarna
hoga: **ghanti bajane se pehle ye batao ki kitni ghantiyan sach hongi.**

## Farz (assumptions) — saaf-saaf likhe hue

Neeche ke numbers JARVIS ke maujooda alert types ke liye hain. Prevalence /
sensitivity / specificity **farz kiye gaye** hain (abhi asli naap nahi hai) —
ye is audit ka kaam hai ki farz ko likh kar dikhaya jaye, taaki kal ko asli
usage data aaye to inhe badla ja sake. Farz badlega to number badlega,
tareeka nahi.

| Alert type | Prevalence (farz) | Sensitivity (farz) | Specificity (farz) |
|---|---|---|---|
| Morning digest | 0.20 | 0.90 | 0.85 |
| Stuck-detection | 0.02 | 0.80 | 0.95 |
| Reminder nudges | 0.05 | 0.90 | 0.90 |
| Provider-down notice | 0.01 | 0.99 | 0.999 |

- **Prevalence** = kisi bhi ek jaanch (glance/check) me wo haal sach me
  maujood hone ka chance. Subah ke candidate items me ~5 me se 1 sach me
  zaroori hota hai (digest 0.20). "Owner sach me atka hua hai" kisi ek
  screen-glance pe nayaab hai (0.02). Provider sach me down hona aur bhi
  nayaab hai (0.01).
- **Sensitivity** = haal sach ho to detector use pakad le.
- **Specificity** = haal na ho to detector chup rahe. Provider probe
  (HTTP/TCP jaanch) isme sabse tez hai (0.999) — woh taqreeban kabhi
  jhooth nahi bolta.

## Nateeja — har ghanti ka sach

Code se nikle hue numbers (`false_alarm_audit`, upar wale farz ke saath):

| Alert type | Ghanti sach hone ka chance (posterior) | Jhooti ghanti (false-alarm rate) | Faisla |
|---|---|---|---|
| Morning digest | 60.0% | 40.0% | Noisy — naram tareeke se dikhao |
| Stuck-detection | 24.6% | 75.4% | Aksar jhooti — interrupt mat karo |
| Reminder nudges | 32.1% | 67.9% | Aksar jhooti — interrupt mat karo |
| Provider-down notice | 90.9% | 9.1% | Theek hai — aam tareeke se batao |

Plain words me:

1. **Morning digest — 10 me se 4 ghantiyan jhooti.** Digest pehle se hi
   ek jagah ikatthi hui khabar hai, wo beech me tokta nahi — is liye 40%
   jhooti ghanti yahan bardasht hai. Lekin digest ke andar "urgent"
   tag lagana isi 60% sach pe chalna chahiye, aur se zyada chillana nahi.
2. **Stuck-detection — 4 me se 3 ghantiyan jhooti.** Screen dekh ke
   "lagta hai aap atke ho" kehna sabse khatarnak nag hai: owner sach me
   atka ho, ye haal hi nayaab hai (2%), aur detector aam hai (95%
   specific). Nateeja: **ye ghanti kabhi bol ke interrupt nahi karegi** —
   sirf log hogi, ya hadd se hadd khamosh ishara (ring model: visual +
   soft tone, awaaz nahi). Bolna hai to detector ko behtar banana hoga
   (specificity 0.95 → 0.99 le jao to posterior 24.6% → 62.0%, aur
   0.995 pe 76.6% ho jata hai; hisaab measure.py se check kiya hua hai).
3. **Reminder nudges — 3 me se 2 jhooti.** "Ye pending hai, kar lo" wale
   nudges ka bhi yahi haal hai. Ye bhi **bol ke nahi** aayenge — digest me
   ya khamosh ring pe rahenge. Jo reminder owner ne khud waqt de ke banaya
   hai woh is audit se bahar hai (woh detector nahi, hukm hai — woh
   hamesha bajega).
4. **Provider-down notice — 10 me se 9 sach.** Ye sabse nayaab haal hai
   (1%) phir bhi ghanti bharosemand hai, kyunki probe ki specificity
   0.999 hai — jhooti ghanti taqreeban hoti hi nahi. Sabak: **nayaab
   haal pe ghanti tabhi chalti hai jab detector taqreeban kabhi jhooth
   na bole.** Ye notice aam tareeke se surface ho sakta hai.

## Is audit ke baad ke niyam (Step 01 ka output)

1. Koi naya proactive alert type tab tak nahi judega jab tak uska
   prevalence/sensitivity/specificity farz likh ke `false_alarm_audit`
   se na guzar jaye.
2. False-alarm rate ≥ 50% wali ghanti **kabhi bol ke interrupt nahi**
   karegi — log ya silent ring hi uski jagah hai (owner ka call-ringing
   model yahi kehta hai).
3. Ghanti ko bolne ke kabil banana hai to raasta number se hai: ya
   detector ki specificity badhao, ya prevalence zyada wale pal chuno
   (masalan stuck-detection sirf lambi khamoshi + same-screen pe chalao)
   — "thoda aur zor se chillao" koi raasta nahi.
4. Jab asli usage data aaye (Step 14 ki eval suite se), farz ki jagah
   asli naap aayega aur ye audit dobara chalega.

## Saboot khud check karna ho to

```
python -m unittest tests.test_measure -v   # 29 tests: posterior, PSI,
                                           # paired-t, test-once kanoon
python -c "from jarvis.measure import posterior; print(posterior(0.0001, 0.99, 0.99))"
# -> 0.0098...  (textbook: 99%-sahi test, phir bhi ~99% ghantiyan jhooti)
```
