"use strict";
// Interface language. Separate from the language of your recordings: this is the
// language of the buttons, that one decides what whisper listens for.

const STRINGS = {
  en: {
    "nav.transcribe": "Transcribe", "nav.library": "Library", "nav.settings": "Settings",
    "kicker": "Audio in, transcript out · nothing leaves this computer",
    "env.ready": "ffmpeg + whisper-cli ready",
    "env.missing": "missing {names}",
    "env.offline": "not running",
    "browse": "Browse",

    // Transcribe
    "new.source": "Source",
    "new.choose": "Choose an audio or video file",
    "new.chooseHint": "MP3, WAV, M4A, MP4, MOV — anything your ffmpeg reads.",
    "new.paste": "or paste a path",
    "new.change": "Change",
    "new.transcription": "Transcription",
    "new.model": "Quality",
    "new.language": "Language",
    "new.elsewhere": "Somewhere else…",
    "new.noModels": "No model found. Put a ggml-*.bin file in ~/whisper-models, or point at one.",
    "new.result": "Result",
    "new.txt": "Transcript · txt", "new.srt": "Subtitles · srt",
    "new.keep": "Keep intermediate audio",
    "new.advanced": "Advanced",
    "new.extra": "Extra whisper-cli arguments",
    "new.extraHint": "Split into separate tokens. Never run through a shell.",
    "new.outFolder": "Output folder", "new.outName": "Output name",
    "new.start": "Start transcription",
    "new.startMany": "Start {n} transcriptions",
    "new.clear": "Clear",
    "new.batch": "+ {n} more queued after this one, each written next to its own file with the same settings.",
    "new.outEmpty": "Choose a file to see where the transcript will be written.",

    // Job
    "job.queued": "Waiting to start", "job.starting": "Getting ready",
    "job.converting": "Preparing the audio", "job.transcribing": "Transcribing",
    "job.saving": "Writing transcript", "job.completed": "Done",
    "job.cancelling": "Stopping", "job.cancelled": "Cancelled", "job.failed": "Failed",
    "job.elapsed": "elapsed", "job.total": "total",
    "job.cancel": "Cancel transcription", "job.again": "Transcribe another file",
    "job.copy": "Copy transcript", "job.copied": "Copied",
    "job.openFolder": "Open folder", "job.log": "Process log",
    "job.details": "Technical details",
    "job.lost": "Lost contact with the app's backend. Reopen the app and start again.",
    "job.cancelConfirm": "Cancel this transcription? The part already transcribed is kept, so you can resume.",
    "job.waiting": "Waiting", "job.remove": "Remove",
    "job.unfinished": "Unfinished transcription",
    "job.reached": "{name} — reached {at}{of}, {was}.",
    "job.resume": "Resume", "job.discard": "Discard",
    "job.discardConfirm": "Discard this run's progress? The part already transcribed is lost.",
    "job.recent": "Recent",
    "th.file": "File", "th.status": "Status", "th.lang": "Lang", "th.finished": "Finished",

    // Library
    "lib.search": "Search every transcript",
    "lib.searchPlaceholder": "a word or phrase",
    "lib.matches": "Matches",
    "lib.transcripts": "Transcripts",
    "lib.empty": "Nothing transcribed yet. The Transcribe view is where that starts.",
    "lib.back": "Back to list",
    "lib.moved": "recording moved",
    "lib.noMedia": "The original recording is no longer where it was, so there is nothing to play.",
    "lib.hits": "{n} match", "lib.hitsPlural": "{n} matches",
    "lib.noHits": "No transcript contains that.",
    "lib.jumpTo": "Jump to {at}",

    // Settings
    "set.basics": "The basics",
    "set.spokenLanguage": "Language spoken in your recordings",
    "set.detect": "Work it out automatically",
    "set.languageHint": "Get this wrong and the transcript is nonsense — Hebrew read as English does not fail, it invents. Whatever you used last time is remembered here.",
    "set.quality": "Quality",
    "set.qualityHint": "Bigger is more accurate and slower.",
    "set.noModels": "No model found",
    "set.modelFound": "Found automatically. Change it under Expert if you keep models elsewhere.",
    "set.modelMissing": "No model found. Put a ggml-*.bin file in ~/whisper-models — see the README for the download command.",
    "set.vocabulary": "Words it keeps getting wrong",
    "set.vocabularyHint": "Names, jargon, product names. Telling it which words to expect makes it reach for them instead of guessing. A couple of lines is plenty; unrelated words make things worse.",
    "set.silence": "Skip silence",
    "set.silenceOn": "Yes — recommended", "set.silenceOff": "No",
    "set.silenceReady": "Silence is skipped, which stops it inventing speech that was never there.",
    "set.silenceMissing": "Needs a small extra model — the command is under Expert.",
    "set.silenceNeedsModel": "Skipping silence needs a small model file first.",
    "set.reading": "Transcript text",
    "set.small": "Small", "set.normal": "Normal", "set.large": "Large", "set.larger": "Larger",
    "set.serif": "Serif", "set.sans": "Sans serif",
    "set.automatic": "Transcribe new recordings on their own",
    "set.watchHint": "Folders listed here are checked every few minutes and anything new inside is transcribed without asking. That uses your graphics card and memory while you are doing something else, so leave this empty unless you want it. It only ever runs while this app is open.",
    "set.addFolder": "Add a folder…", "set.queueFolder": "Transcribe a folder now…",
    "set.looking": "Looking…",
    "set.queuedN": "Queued {n}: {names}",
    "set.queuedNone": "Nothing new to transcribe there.",
    "set.expert": "Expert",
    "set.modelFile": "Model file",
    "set.silenceModel": "Silence-detection model",
    "set.extraArgs": "Extra whisper-cli arguments",
    "set.toolsHint": "Leave the three above empty and it finds them by itself, which is what normally happens.",
    "set.backup": "Backup",
    "set.backupHint": "Everything on this screen, as a file you can keep or move to another computer. Your transcripts are not included — they are already files, sitting next to your recordings.",
    "set.export": "Save settings to a file", "set.import": "Load settings from a file",
    "set.exported": "Saved.", "set.imported": "Settings loaded.",
    "set.importNotJson": "That file is not settings — it is not even JSON.",
    "set.importWrongFile": "That is a JSON file, but not one of ours.",
    "set.save": "Save settings", "set.saved": "Saved.",
    "set.clearHistory": "Clear the list of past transcriptions",
    "set.clearConfirm": "Clear the list of past transcriptions?\n\nThe transcript files themselves are not touched.",

    "quality.best": "Best", "quality.good": "Good",
    "quality.quick": "Quick", "quality.roughest": "Roughest",
  },

  he: {
    "nav.transcribe": "תמלול", "nav.library": "ספרייה", "nav.settings": "הגדרות",
    "kicker": "הקלטה נכנסת, תמליל יוצא · שום דבר לא יוצא מהמחשב הזה",
    "env.ready": "ffmpeg ו‑whisper‑cli מוכנים",
    "env.missing": "חסר: {names}",
    "env.offline": "לא פועל",
    "browse": "עיון",

    "new.source": "מקור",
    "new.choose": "בחרו קובץ אודיו או וידאו",
    "new.chooseHint": "‏MP3, WAV, M4A, MP4, MOV — כל מה ש‑ffmpeg יודע לקרוא.",
    "new.paste": "או הדביקו נתיב",
    "new.change": "החלפה",
    "new.transcription": "תמלול",
    "new.model": "איכות",
    "new.language": "שפה",
    "new.elsewhere": "במקום אחר…",
    "new.noModels": "לא נמצא מודל. שימו קובץ ‎ggml-*.bin בתיקייה ‎~/whisper-models, או הצביעו על אחד.",
    "new.result": "התוצאה",
    "new.txt": "תמליל · txt", "new.srt": "כתוביות · srt",
    "new.keep": "שמירת קובץ האודיו הזמני",
    "new.advanced": "מתקדם",
    "new.extra": "ארגומנטים נוספים ל‑whisper-cli",
    "new.extraHint": "מפוצלים לאסימונים נפרדים. אף פעם לא עוברים דרך מעטפת.",
    "new.outFolder": "תיקיית יעד", "new.outName": "שם הקובץ",
    "new.start": "התחלת תמלול",
    "new.startMany": "התחלת {n} תמלולים",
    "new.clear": "ניקוי",
    "new.batch": "‏+{n} נוספים בתור אחרי זה, כל אחד נשמר ליד הקובץ שלו ובאותן הגדרות.",
    "new.outEmpty": "בחרו קובץ כדי לראות היכן ייכתב התמליל.",

    "job.queued": "ממתין להתחלה", "job.starting": "מתארגן",
    "job.converting": "מכין את האודיו", "job.transcribing": "מתמלל",
    "job.saving": "כותב את התמליל", "job.completed": "הסתיים",
    "job.cancelling": "עוצר", "job.cancelled": "בוטל", "job.failed": "נכשל",
    "job.elapsed": "עבר", "job.total": "סה״כ",
    "job.cancel": "ביטול התמלול", "job.again": "תמלול קובץ נוסף",
    "job.copy": "העתקת התמליל", "job.copied": "הועתק",
    "job.openFolder": "פתיחת התיקייה", "job.log": "יומן התהליך",
    "job.details": "פרטים טכניים",
    "job.lost": "אבד הקשר לשרת של האפליקציה. פתחו אותה מחדש והתחילו שוב.",
    "job.cancelConfirm": "לבטל את התמלול? מה שכבר תומלל נשמר, כך שאפשר להמשיך אחר כך.",
    "job.waiting": "בתור", "job.remove": "הסרה",
    "job.unfinished": "תמלול שלא הסתיים",
    "job.reached": "{name} — הגיע ל‑{at}{of}, {was}.",
    "job.resume": "המשך", "job.discard": "מחיקה",
    "job.discardConfirm": "למחוק את ההתקדמות של התמלול הזה? מה שכבר תומלל יאבד.",
    "job.recent": "אחרונים",
    "th.file": "קובץ", "th.status": "מצב", "th.lang": "שפה", "th.finished": "הסתיים",

    "lib.search": "חיפוש בכל התמלילים",
    "lib.searchPlaceholder": "מילה או ביטוי",
    "lib.matches": "תוצאות",
    "lib.transcripts": "תמלילים",
    "lib.empty": "עדיין לא תומלל דבר. מתחילים במסך התמלול.",
    "lib.back": "חזרה לרשימה",
    "lib.moved": "ההקלטה הוזזה",
    "lib.noMedia": "ההקלטה המקורית כבר לא נמצאת במקומה, ואין מה להשמיע.",
    "lib.hits": "תוצאה אחת", "lib.hitsPlural": "{n} תוצאות",
    "lib.noHits": "אין תמליל שמכיל את זה.",
    "lib.jumpTo": "מעבר ל‑{at}",

    "set.basics": "העיקר",
    "set.spokenLanguage": "השפה המדוברת בהקלטות שלכם",
    "set.detect": "לזהות לבד",
    "set.languageHint": "טעות כאן הופכת את התמליל לג׳יבריש — עברית שנקראת כאנגלית לא נכשלת, היא ממציאה. מה שהשתמשתם בו בפעם הקודמת נשמר כאן.",
    "set.quality": "איכות",
    "set.qualityHint": "גדול יותר — מדויק יותר ואיטי יותר.",
    "set.noModels": "לא נמצא מודל",
    "set.modelFound": "נמצא אוטומטית. אפשר לשנות תחת ״מומחים״ אם המודלים שלכם במקום אחר.",
    "set.modelMissing": "לא נמצא מודל. שימו קובץ ‎ggml-*.bin בתיקייה ‎~/whisper-models — פקודת ההורדה נמצאת ב‑README.",
    "set.vocabulary": "מילים שהוא כל הזמן טועה בהן",
    "set.vocabularyHint": "שמות, ז׳רגון, שמות מוצרים. כשאומרים לו אילו מילים לצפות, הוא מושך אליהן במקום לנחש. שתי שורות מספיקות; מילים שלא קשורות להקלטה רק מזיקות.",
    "set.silence": "דילוג על שקט",
    "set.silenceOn": "כן — מומלץ", "set.silenceOff": "לא",
    "set.silenceReady": "מדלג על שקט, מה שמונע ממנו להמציא דיבור שלא היה.",
    "set.silenceMissing": "דורש מודל קטן נוסף — הפקודה נמצאת תחת ״מומחים״.",
    "set.silenceNeedsModel": "כדי לדלג על שקט צריך קודם קובץ מודל קטן.",
    "set.reading": "טקסט התמליל",
    "set.small": "קטן", "set.normal": "רגיל", "set.large": "גדול", "set.larger": "גדול יותר",
    "set.serif": "סריף", "set.sans": "ללא סריף",
    "set.automatic": "תמלול הקלטות חדשות מעצמו",
    "set.watchHint": "תיקיות שמופיעות כאן נבדקות כל כמה דקות, וכל דבר חדש בתוכן מתומלל בלי לשאול. זה מנצל את כרטיס המסך והזיכרון בזמן שאתם עושים דברים אחרים, אז השאירו ריק אלא אם אתם רוצים בכך. זה קורה רק כשהאפליקציה פתוחה.",
    "set.addFolder": "הוספת תיקייה…", "set.queueFolder": "תמלול תיקייה עכשיו…",
    "set.looking": "מחפש…",
    "set.queuedN": "נוספו לתור {n}: {names}",
    "set.queuedNone": "אין שם שום דבר חדש לתמלל.",
    "set.expert": "מומחים",
    "set.modelFile": "קובץ המודל",
    "set.silenceModel": "מודל לזיהוי שקט",
    "set.extraArgs": "ארגומנטים נוספים ל‑whisper-cli",
    "set.toolsHint": "אם תשאירו את שלושת אלה ריקים, הוא ימצא אותם לבד — וכך קורה בדרך כלל.",
    "set.backup": "גיבוי",
    "set.backupHint": "כל מה שבמסך הזה, כקובץ שאפשר לשמור או להעביר למחשב אחר. התמלילים עצמם לא נכללים — הם ממילא קבצים, שיושבים ליד ההקלטות.",
    "set.export": "שמירת ההגדרות לקובץ", "set.import": "טעינת הגדרות מקובץ",
    "set.exported": "נשמר.", "set.imported": "ההגדרות נטענו.",
    "set.importNotJson": "הקובץ הזה אינו הגדרות — הוא אפילו לא JSON.",
    "set.importWrongFile": "זה קובץ JSON, אבל לא שלנו.",
    "set.save": "שמירת ההגדרות", "set.saved": "נשמר.",
    "set.clearHistory": "ניקוי רשימת התמלולים הקודמים",
    "set.clearConfirm": "לנקות את רשימת התמלולים הקודמים?\n\nקבצי התמלילים עצמם לא נוגעים בהם.",

    "quality.best": "הטוב ביותר", "quality.good": "טוב",
    "quality.quick": "מהיר", "quality.roughest": "גס",
  },
};

const RTL_UI = new Set(["he"]);

let LANG = localStorage.getItem("lwt.ui_language") ||
  (navigator.language || "en").slice(0, 2).toLowerCase();
if (!STRINGS[LANG]) LANG = "en";

function t(key, vars) {
  let text = (STRINGS[LANG] && STRINGS[LANG][key]) || STRINGS.en[key] || key;
  if (vars) for (const [k, v] of Object.entries(vars)) text = text.replaceAll(`{${k}}`, v);
  return text;
}

function applyTranslations(root) {
  for (const el of (root || document).querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const el of (root || document).querySelectorAll("[data-i18n-placeholder]")) {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  }
  document.documentElement.lang = LANG;
  document.documentElement.dir = RTL_UI.has(LANG) ? "rtl" : "ltr";
  for (const btn of document.querySelectorAll("#lang-switch [data-lang]")) {
    btn.setAttribute("aria-pressed", String(btn.dataset.lang === LANG));
  }
}

function setLanguage(lang) {
  if (!STRINGS[lang] || lang === LANG) return;
  LANG = lang;
  localStorage.setItem("lwt.ui_language", lang);
  applyTranslations();
  // Anything drawn by script has to be drawn again in the new language.
  if (typeof lastState !== "undefined" && lastState) render(lastState);
  if (typeof openSettings === "function" && currentView() === "settings") openSettings();
  if (typeof openLibrary === "function" && currentView() === "library") openLibrary();
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-lang]");
  if (btn) setLanguage(btn.dataset.lang);
});
