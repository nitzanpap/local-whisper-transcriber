"use strict";
// Interface language. Separate from the language of your recordings: this is the
// language of the buttons, that one decides what whisper listens for.

const STRINGS = {
  en: {
    "nav.settings": "Settings", "nav.back": "Back", "nav.language": "Interface language",
    "kicker": "Audio in, transcript out · nothing leaves this computer",
    "start.what": "What do you want words from?",
    "env.ready": "ffmpeg + whisper-cli ready",
    "env.missing": "missing {names}",
    "env.offline": "not running",
    "confirm.yes": "Yes",
    "confirm.no": "Cancel",
    "browse": "Browse",

    // Transcribe
    "new.source": "Source",
    "new.choose": "Choose an audio or video file",
    "new.chooseHint": "MP3, WAV, M4A, MP4, MOV — anything your ffmpeg reads.",
    "new.paste": "or paste a path",
    "new.change": "Change",
    "new.model": "Quality",
    "new.language": "Language",
    "new.elsewhere": "Somewhere else…",
    "new.noModels": "No model found. Put a ggml-*.bin file in ~/whisper-models, or point at one.",
    "new.txt": "Transcript · txt", "new.srt": "Subtitles · srt",
    "new.keep": "Keep intermediate audio",
    "new.advanced": "Advanced",
    "new.extra": "Extra whisper-cli arguments",
    "new.extraHint": "Split into separate tokens. Never run through a shell.",
    "new.outFolder": "Output folder", "new.outName": "Output name",
    "new.start": "Transcribe",
    "new.startMany": "Transcribe {n} files",
    "new.other": "Choose something else",
    "new.changeHow": "Change how",
    "new.batch": "+ {n} more queued after this one, each written next to its own file with the same settings.",
    "new.outEmpty": "Choose a file to see where the transcript will be written.",

    // Record
    "rec.sources": "What to record",
    "rec.voice": "Your voice",
    "rec.computer": "Your computer's audio",
    "rec.nothing": "Nothing",
    "rec.start": "Record",
    "rec.startHint": "A meeting, a call, anything playing on this Mac.",
    "rec.refresh": "Look again",
    "rec.stop": "Stop",
    "rec.throw": "Stop and throw it away",
    "rec.throwConfirm": "Stop recording and delete what was recorded? This cannot be undone.",
    "rec.clear": "Dismiss",
    "rec.recorded": "recorded",
    "rec.planBoth": "one file, {voice} on the left and {computer} on the right",
    "rec.planOne": "one file, a single voice, no speaker labels",
    "rec.planNothing": "Choose at least one thing to record.",
    "rec.twoChannels": "{voice} + {computer}, kept apart",
    "rec.oneChannel": "one source",
    "rec.stopsAfter": "stops by itself after {n} min",
    "rec.status.recording": "Recording",
    "rec.status.stopping": "Finishing the file",
    "rec.status.saving": "Saving",
    "rec.savedTitle": "Recorded {at}",
    "rec.needLoopbackTitle": "Your computer's audio cannot be recorded yet",
    "rec.needLoopbackWhat": "macOS offers apps the microphone and nothing else — there is no " +
      "input device carrying what your speakers are playing until you install one. Your voice " +
      "alone will record fine in the meantime.",
    "rec.noDevicesTitle": "No audio inputs found",
    "rec.noDevicesWhat": "ffmpeg listed no recording devices at all. On macOS this usually " +
      "means the app has not been allowed to use the microphone yet: System Settings → " +
      "Privacy & Security → Microphone.",
    "rec.quietVoice": "Nothing audible was recorded from your microphone. Check that " +
      "it is allowed under System Settings → Privacy & Security → Microphone, that the " +
      "right input is selected, and that it is not muted or asleep.",
    "rec.quietComputer": "Nothing audible was recorded from your computer. The usual " +
      "reason is permission: System Settings → Privacy & Security → System Audio " +
      "Recording Only. A refusal there is silent — the recording runs perfectly and " +
      "captures nothing. Otherwise check that sound was really playing, and that the " +
      "output is not muted.",
    "rec.systemAudio": "System audio (no driver needed)",
    "rec.howTo": "How to set that up",
    "rec.loopbackSteps":
      "1. Install a loopback driver:\n" +
      "     brew install blackhole-2ch\n\n" +
      "2. Open Audio MIDI Setup and make a Multi-Output Device.\n" +
      "   Tick your speakers or headphones AND BlackHole 2ch.\n" +
      "   Put the built-in output at the top as the clock source, and\n" +
      "   turn on Drift Correction for BlackHole. Set both to 48000 Hz.\n\n" +
      "3. In System Settings → Sound, choose that Multi-Output Device\n" +
      "   as your output. You still hear everything; BlackHole now\n" +
      "   receives a copy.\n\n" +
      "4. Come back here and press Look again. BlackHole 2ch will be\n" +
      "   in the second dropdown.\n\n" +
      "You do NOT need an Aggregate Device. One concatenates channels\n" +
      "instead of mixing them, which is why recorders fed one come back\n" +
      "with the microphone alone. The mixing happens here instead.",
    "rec.orphanTitle": "Recording that was never saved",
    "rec.orphanWhat": "{at} of audio ({size}) was captured but never written out — the app " +
      "stopped before it could be.",
    "rec.orphanKeep": "Save it",
    "rec.orphanDropConfirm": "Throw this recording away? The audio is lost.",

    // Job
    "job.track": "{stage} · {label} ({n} of {of})",
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
    "lib.empty": "Nothing transcribed yet. Record something, or choose a file.",
    "lib.back": "Back to list",
    "lib.moved": "recording moved",
    "lib.noMedia": "The original recording is no longer where it was, so there is nothing to play.",
    "lib.hits": "{n} match", "lib.hitsPlural": "{n} matches",
    "lib.noHits": "No transcript contains that.",
    "lib.jumpTo": "Jump to {at}",

    // Settings
    "set.refine": "Refinements",
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
    // Serif and Sans serif are left alone: type terms travel as they are.
    "set.small": "Small", "set.normal": "Normal", "set.large": "Large", "set.larger": "Larger",
    "set.automatic": "Transcribe new recordings on their own",
    "set.watchHint": "Folders listed here are checked every few minutes and anything new inside is transcribed without asking. That uses your graphics card and memory while you are doing something else, so leave this empty unless you want it. It only ever runs while this app is open.",
    "set.addFolder": "Add a folder…", "set.queueFolder": "Transcribe a folder now…",
    "set.looking": "Looking…",
    "set.queuedN": "Queued {n}: {names}",
    "set.queuedNone": "Nothing new to transcribe there.",
    "set.recording": "Recording",
    "set.recordingHint": "What happens when you press Record. Your voice and " +
      "your computer's audio are kept in separate channels of one file, which is what lets the " +
      "transcript say who said each line.",
    "set.recFolder": "Where recordings go",
    "set.recLabelVoice": "What to call you in the transcript",
    "set.recLabelComputer": "What to call everyone else",
    "set.recLabelsHint": "Used only when both sources are recorded, because only then is it " +
      "known who said which line.",
    "set.recAuto": "When a recording stops",
    "set.recAutoOn": "Transcribe it straight away",
    "set.recAutoOff": "Just keep the file",
    "set.recMax": "Stop by itself after",
    "set.recMaxHint": "Minutes. A recording nobody stopped would fill the disk.",
    "set.expert": "Expert",
    "set.modelFile": "Model file",
    "set.silenceModel": "Silence-detection model",
    "set.extraArgs": "Extra whisper-cli arguments",
    "set.toolsHint": "Leave the three above empty and it finds them by itself, which is what normally happens.",
    "set.backup": "Backup",
    "set.backupHint": "Everything on this screen, as a file you can keep or move to another computer. Your transcripts are not included — they are already files, sitting next to your recordings.",
    "set.export": "Save settings to a file", "set.import": "Load settings from a file",
    "set.exported": "Saved to {path}", "set.imported": "Settings loaded from {path}",
    "set.exportCancelled": "Nothing saved.",
    "set.importNotJson": "That file is not settings — it is not even JSON.",
    "set.importWrongFile": "That is a JSON file, but not one of ours.",
    "set.save": "Save settings", "set.saved": "Saved.",
    "set.clearHistory": "Clear the list of past transcriptions",
    "set.clearConfirm": "Clear the list of past transcriptions?\n\nThe transcript files themselves are not touched.",

    "lib.details": "How this was made",
    "fact.took": "Time taken", "fact.audio": "Recording length",
    "fact.speed": "Speed", "fact.speedValue": "{n}× faster than real time",
    "fact.cpu": "Processor time", "fact.memory": "Peak memory",
    "fact.model": "Model", "fact.language": "Language",
    "fact.silence": "Silence skipped", "fact.vocabulary": "Vocabulary",
    "fact.args": "Extra arguments", "fact.when": "Finished",
    "fact.yes": "yes", "fact.no": "no", "fact.unknown": "not recorded",
    "pending.title": "New recordings",
    "pending.what": "{n} in your source folders have no transcript yet: {names}",
    "pending.go": "Transcribe them", "pending.later": "Not now",
    "pending.none": "Nothing new in your source folders.",
    "picker.opening": "Opening…",
    "set.sources": "Where your recordings are",
    "set.sourcesHint": "Folders you record into — Zoom, Meet, voice memos, anywhere. When you open the app it looks once and offers to transcribe anything new. It never looks while you are away.",
    "set.output": "Where transcripts go",
    "set.outputBeside": "Next to each recording",
    "set.outputFolder": "All in one folder",
    "set.checkNow": "Check for new recordings now",
    "quality.best": "Best", "quality.good": "Good",
    "quality.quick": "Quick", "quality.roughest": "Roughest",
  },

  he: {
    "nav.settings": "הגדרות", "nav.back": "חזרה", "nav.language": "שפת הממשק",
    "kicker": "הקלטה נכנסת, תמליל יוצא · שום דבר לא יוצא מהמחשב הזה",
    "start.what": "ממה תרצו טקסט?",
    "env.ready": "ffmpeg ו‑whisper‑cli מוכנים",
    "env.missing": "חסר: {names}",
    "env.offline": "לא פועל",
    "confirm.yes": "כן",
    "confirm.no": "ביטול",
    "browse": "עיון",

    "new.source": "מקור",
    "new.choose": "בחרו קובץ אודיו או וידאו",
    "new.chooseHint": "‏MP3, WAV, M4A, MP4, MOV — כל מה ש‑ffmpeg יודע לקרוא.",
    "new.paste": "או הדביקו נתיב",
    "new.change": "החלפה",
    "new.model": "איכות",
    "new.language": "שפה",
    "new.elsewhere": "במקום אחר…",
    "new.noModels": "לא נמצא מודל. שימו קובץ ‎ggml-*.bin בתיקייה ‎~/whisper-models, או הצביעו על אחד.",
    "new.txt": "תמליל · txt", "new.srt": "כתוביות · srt",
    "new.keep": "שמירת קובץ האודיו הזמני",
    "new.advanced": "מתקדם",
    "new.extra": "ארגומנטים נוספים ל‑whisper-cli",
    "new.extraHint": "מפוצלים לאסימונים נפרדים. אף פעם לא עוברים דרך מעטפת.",
    "new.outFolder": "תיקיית יעד", "new.outName": "שם הקובץ",
    "new.start": "לתמלל",
    "new.startMany": "‏לתמלל {n} קבצים",
    "new.other": "לבחור משהו אחר",
    "new.changeHow": "לשנות איך",
    "new.batch": "‏+{n} נוספים בתור אחרי זה, כל אחד נשמר ליד הקובץ שלו ובאותן הגדרות.",
    "new.outEmpty": "בחרו קובץ כדי לראות היכן ייכתב התמליל.",

    "rec.sources": "מה להקליט",
    "rec.voice": "הקול שלכם",
    "rec.computer": "האודיו של המחשב",
    "rec.nothing": "כלום",
    "rec.start": "להקליט",
    "rec.startHint": "פגישה, שיחה, כל דבר שמתנגן במחשב הזה.",
    "rec.refresh": "לבדוק שוב",
    "rec.stop": "עצירה",
    "rec.throw": "עצירה ומחיקה",
    "rec.throwConfirm": "לעצור את ההקלטה ולמחוק את מה שהוקלט? אין דרך לשחזר.",
    "rec.clear": "לסגור",
    "rec.recorded": "הוקלט",
    "rec.planBoth": "‏קובץ אחד, {voice} בשמאל ו‑{computer} בימין",
    "rec.planOne": "קובץ אחד, קול אחד, בלי סימון דוברים",
    "rec.planNothing": "בחרו לפחות דבר אחד להקליט.",
    "rec.twoChannels": "‏{voice} + {computer}, בנפרד",
    "rec.oneChannel": "מקור אחד",
    "rec.stopsAfter": "‏נעצרת לבד אחרי {n} דק׳",
    "rec.status.recording": "מקליט",
    "rec.status.stopping": "מסיים את הקובץ",
    "rec.status.saving": "שומר",
    "rec.savedTitle": "‏הוקלטו {at}",
    "rec.needLoopbackTitle": "עדיין אי אפשר להקליט את האודיו של המחשב",
    "rec.needLoopbackWhat": "‏macOS מציע לאפליקציות את המיקרופון וזה הכול — אין התקן קלט " +
      "שמעביר את מה שהרמקולים מנגנים עד שמתקינים אחד. בינתיים הקול שלכם לבד יוקלט בסדר גמור.",
    "rec.noDevicesTitle": "לא נמצאו התקני קלט",
    "rec.noDevicesWhat": "‏ffmpeg לא מצא שום התקן הקלטה. ב‑macOS זה בדרך כלל אומר שלא ניתנה " +
      "לאפליקציה הרשאה למיקרופון: הגדרות המערכת → פרטיות ואבטחה → מיקרופון.",
    "rec.quietVoice": "לא הוקלט שום דבר שנשמע מהמיקרופון. בדקו שיש לו הרשאה תחת " +
      "הגדרות המערכת ← פרטיות ואבטחה ← מיקרופון, שנבחר הקלט הנכון, ושהוא לא מושתק או רדום.",
    "rec.quietComputer": "לא הוקלט שום דבר שנשמע מהמחשב. הסיבה הרגילה היא הרשאה: " +
      "הגדרות המערכת ← פרטיות ואבטחה ← ‏System Audio Recording Only. סירוב שם הוא שקט — " +
      "ההקלטה רצה מצוין ולא קולטת כלום. אחרת בדקו שבאמת התנגן צליל, ושהפלט לא מושתק.",
    "rec.systemAudio": "האודיו של המחשב (בלי דרייבר)",
    "rec.howTo": "איך מגדירים את זה",
    "rec.loopbackSteps":
      "1. התקינו דרייבר loopback:\n" +
      "     brew install blackhole-2ch\n\n" +
      "2. פתחו Audio MIDI Setup וצרו Multi-Output Device.\n" +
      "   סמנו את הרמקולים או האוזניות שלכם וגם BlackHole 2ch.\n" +
      "   שימו את הפלט המובנה בראש הרשימה כמקור השעון,\n" +
      "   והפעילו Drift Correction ל‑BlackHole. שניהם ב‑48000 Hz.\n\n" +
      "3. בהגדרות המערכת → סאונד, בחרו את ה‑Multi-Output Device\n" +
      "   כפלט. אתם ממשיכים לשמוע הכול; BlackHole מקבל עותק.\n\n" +
      "4. חזרו לכאן ולחצו ״לבדוק שוב״. BlackHole 2ch יופיע\n" +
      "   בתפריט השני.\n\n" +
      "אין צורך ב‑Aggregate Device. הוא משרשר ערוצים ולא מערבב\n" +
      "אותם, ולכן מקליטים שמאכילים אותו מחזירים את המיקרופון לבד.\n" +
      "הערבוב קורה כאן במקום.",
    "rec.orphanTitle": "הקלטה שלא נשמרה",
    "rec.orphanWhat": "‏{at} של אודיו ({size}) הוקלטו אבל לא נכתבו לקובץ — האפליקציה נעצרה לפני.",
    "rec.orphanKeep": "לשמור אותה",
    "rec.orphanDropConfirm": "למחוק את ההקלטה הזאת? האודיו יאבד.",

    "job.track": "‏{stage} · {label} ({n} מתוך {of})",
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
    "lib.empty": "עדיין לא תומלל דבר. הקליטו משהו, או בחרו קובץ.",
    "lib.back": "חזרה לרשימה",
    "lib.moved": "ההקלטה הוזזה",
    "lib.noMedia": "ההקלטה המקורית כבר לא נמצאת במקומה, ואין מה להשמיע.",
    "lib.hits": "תוצאה אחת", "lib.hitsPlural": "{n} תוצאות",
    "lib.noHits": "אין תמליל שמכיל את זה.",
    "lib.jumpTo": "מעבר ל‑{at}",

    "set.refine": "עידונים",
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
    "set.automatic": "תמלול הקלטות חדשות מעצמו",
    "set.watchHint": "תיקיות שמופיעות כאן נבדקות כל כמה דקות, וכל דבר חדש בתוכן מתומלל בלי לשאול. זה מנצל את כרטיס המסך והזיכרון בזמן שאתם עושים דברים אחרים, אז השאירו ריק אלא אם אתם רוצים בכך. זה קורה רק כשהאפליקציה פתוחה.",
    "set.addFolder": "הוספת תיקייה…", "set.queueFolder": "תמלול תיקייה עכשיו…",
    "set.looking": "מחפש…",
    "set.queuedN": "נוספו לתור {n}: {names}",
    "set.queuedNone": "אין שם שום דבר חדש לתמלל.",
    "set.recording": "הקלטה",
    "set.recordingHint": "מה שקורה כשלוחצים על ״להקליט״. הקול שלכם והאודיו של המחשב " +
      "נשמרים בערוצים נפרדים של אותו קובץ, וזה מה שמאפשר לתמליל לומר מי אמר כל שורה.",
    "set.recFolder": "לאן הולכות ההקלטות",
    "set.recLabelVoice": "איך לקרוא לכם בתמליל",
    "set.recLabelComputer": "איך לקרוא לכל השאר",
    "set.recLabelsHint": "בשימוש רק כששני המקורות מוקלטים, כי רק אז ידוע מי אמר איזו שורה.",
    "set.recAuto": "כשהקלטה נעצרת",
    "set.recAutoOn": "לתמלל אותה מיד",
    "set.recAutoOff": "רק לשמור את הקובץ",
    "set.recMax": "להיעצר לבד אחרי",
    "set.recMaxHint": "דקות. הקלטה שאף אחד לא עצר תמלא את הדיסק.",
    "set.expert": "מומחים",
    "set.modelFile": "קובץ המודל",
    "set.silenceModel": "מודל לזיהוי שקט",
    "set.extraArgs": "ארגומנטים נוספים ל‑whisper-cli",
    "set.toolsHint": "אם תשאירו את שלושת אלה ריקים, הוא ימצא אותם לבד — וכך קורה בדרך כלל.",
    "set.backup": "גיבוי",
    "set.backupHint": "כל מה שבמסך הזה, כקובץ שאפשר לשמור או להעביר למחשב אחר. התמלילים עצמם לא נכללים — הם ממילא קבצים, שיושבים ליד ההקלטות.",
    "set.export": "שמירת ההגדרות לקובץ", "set.import": "טעינת הגדרות מקובץ",
    "set.exported": "נשמר אל {path}", "set.imported": "ההגדרות נטענו מ‑{path}",
    "set.exportCancelled": "לא נשמר דבר.",
    "set.importNotJson": "הקובץ הזה אינו הגדרות — הוא אפילו לא JSON.",
    "set.importWrongFile": "זה קובץ JSON, אבל לא שלנו.",
    "set.save": "שמירת ההגדרות", "set.saved": "נשמר.",
    "set.clearHistory": "ניקוי רשימת התמלולים הקודמים",
    "set.clearConfirm": "לנקות את רשימת התמלולים הקודמים?\n\nקבצי התמלילים עצמם לא נוגעים בהם.",

    "lib.details": "איך זה נוצר",
    "fact.took": "זמן שלקח", "fact.audio": "אורך ההקלטה",
    "fact.speed": "מהירות", "fact.speedValue": "פי {n} מהזמן האמיתי",
    "fact.cpu": "זמן מעבד", "fact.memory": "שיא זיכרון",
    "fact.model": "מודל", "fact.language": "שפה",
    "fact.silence": "דילוג על שקט", "fact.vocabulary": "אוצר מילים",
    "fact.args": "ארגומנטים נוספים", "fact.when": "הסתיים",
    "fact.yes": "כן", "fact.no": "לא", "fact.unknown": "לא נרשם",
    "pending.title": "הקלטות חדשות",
    "pending.what": "‏{n} בתיקיות המקור עדיין בלי תמליל: {names}",
    "pending.go": "לתמלל אותן", "pending.later": "לא עכשיו",
    "pending.none": "אין שום דבר חדש בתיקיות המקור.",
    "picker.opening": "נפתח…",
    "set.sources": "איפה ההקלטות שלכם",
    "set.sourcesHint": "התיקיות שאליהן אתם מקליטים — זום, מיט, הקלטות קוליות, כל מקום. כשפותחים את האפליקציה היא מסתכלת פעם אחת ומציעה לתמלל כל דבר חדש. היא לא מסתכלת כשאתם לא כאן.",
    "set.output": "לאן הולכים התמלילים",
    "set.outputBeside": "ליד כל הקלטה",
    "set.outputFolder": "הכול בתיקייה אחת",
    "set.checkNow": "לבדוק עכשיו אם יש הקלטות חדשות",
    "quality.best": "הטוב ביותר", "quality.good": "טוב",
    "quality.quick": "מהיר", "quality.roughest": "גס",
  },
};

// Shown in the globe menu. Adding a language means adding a block above and a
// line here — nothing else changes.
const LANGUAGE_NAMES = { en: "English", he: "עברית" };
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
  // A mark on its own still has to be able to say what it is — on hover for anyone
  // pointing at it, and to a screen reader, which has nothing else to go on.
  for (const el of (root || document).querySelectorAll("[data-i18n-title]")) {
    el.title = t(el.dataset.i18nTitle);
    el.setAttribute("aria-label", el.title);
  }
  document.documentElement.lang = LANG;
  document.documentElement.dir = RTL_UI.has(LANG) ? "rtl" : "ltr";
  const menu = document.getElementById("lang");
  if (menu && !menu.options.length) {
    menu.innerHTML = Object.entries(LANGUAGE_NAMES)
      .map(([code, name]) => `<option value="${code}">${name}</option>`).join("");
  }
  if (menu) menu.value = LANG;
}

function setLanguage(lang) {
  if (!STRINGS[lang] || lang === LANG) return;
  LANG = lang;
  localStorage.setItem("lwt.ui_language", lang);
  applyTranslations();
  // Anything drawn by script has to be drawn again in the new language.
  if (typeof lastState !== "undefined" && lastState) render(lastState);
  if (typeof openSettings === "function" && currentView() === "settings") openSettings();
  if (typeof openLibrary === "function") openLibrary();  // always on screen now
  if (typeof redrawRecord === "function") redrawRecord();
}

document.addEventListener("change", (e) => {
  if (e.target.id === "lang") setLanguage(e.target.value);
});
