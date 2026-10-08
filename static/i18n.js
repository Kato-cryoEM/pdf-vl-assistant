// 画面の文字を訳す言語に合わせて差し替える。
//
// 辞書のキーは日本語の文字列そのもの。キー名を別に考えなくて済み、辞書に無い
// 文字列は日本語のまま出る (壊れない) ので、後から少しずつ追加できる。
//
// 注意: 訳文パネル・チャット・PDF 上の文字は「文書の中身」なので触らない
// (I18N_SKIP を参照)。

const I18N_SKIP = ['jaFlow', 'chatMessages', 'pdfBody', 'pageOverlay', 'commentsBody'];

const I18N = {
  // --- ヘッダ ---
  '📂 保存ジョブ':        {en: '📂 Saved jobs', zh: '📂 已保存任务', ko: '📂 저장된 작업', de: '📂 Gespeicherte Jobs', fr: '📂 Travaux enregistrés', es: '📂 Trabajos guardados'},
  '読込':                 {en: 'Load', zh: '载入', ko: '불러오기', de: 'Laden', fr: 'Charger', es: 'Cargar'},
  '削除':                 {en: 'Delete', zh: '删除', ko: '삭제', de: 'Löschen', fr: 'Supprimer', es: 'Eliminar'},
  '💬 コメント一覧':      {en: '💬 Comments', zh: '💬 批注列表', ko: '💬 코멘트 목록', de: '💬 Kommentare', fr: '💬 Commentaires', es: '💬 Comentarios'},
  '📎 サプリ追加':        {en: '📎 Add supplement', zh: '📎 添加补充材料', ko: '📎 부록 추가', de: '📎 Supplement hinzufügen', fr: '📎 Ajouter un supplément', es: '📎 Añadir suplemento'},
  '🧠 モデル管理':        {en: '🧠 Models', zh: '🧠 模型管理', ko: '🧠 모델 관리', de: '🧠 Modelle', fr: '🧠 Modèles', es: '🧠 Modelos'},
  '準備完了':             {en: 'Ready', zh: '就绪', ko: '준비 완료', de: 'Bereit', fr: 'Prêt', es: 'Listo'},
  'サプリメンタル PDF を追加解析': {en: 'Analyse a supplementary PDF as well', zh: '追加解析补充材料 PDF', ko: '부록 PDF를 추가로 해석', de: 'Ein ergänzendes PDF zusätzlich analysieren', fr: 'Analyser aussi un PDF supplémentaire', es: 'Analizar también un PDF suplementario'},
  '使用する LLM の確認・切替・追加': {en: 'Check, switch or add the LLM in use', zh: '查看、切换或添加所用的 LLM', ko: '사용 중인 LLM 확인・전환・추가', de: 'Verwendetes LLM prüfen, wechseln oder hinzufügen', fr: 'Vérifier, changer ou ajouter le LLM utilisé', es: 'Ver, cambiar o añadir el LLM en uso'},

  // --- アップロード ---
  'モデル':               {en: 'Model', zh: '模型', ko: '모델', de: 'Modell', fr: 'Modèle', es: 'Modelo'},
  '訳す言語':             {en: 'Translate into', zh: '译文语言', ko: '번역 언어', de: 'Übersetzen nach', fr: 'Traduire en', es: 'Traducir a'},
  '最大ページ数 (0=全)':  {en: 'Max pages (0 = all)', zh: '最大页数（0＝全部）', ko: '최대 페이지 수 (0=전체)', de: 'Max. Seiten (0 = alle)', fr: 'Pages max (0 = toutes)', es: 'Páginas máx. (0 = todas)'},
  '解析開始':             {en: 'Start', zh: '开始解析', ko: '해석 시작', de: 'Starten', fr: 'Démarrer', es: 'Iniciar'},
  '中止':                 {en: 'Stop', zh: '中止', ko: '중지', de: 'Abbrechen', fr: 'Arrêter', es: 'Detener'},
  'PDFをドラッグ&ドロップ可': {en: 'You can drag and drop a PDF here', zh: '可拖放 PDF 至此', ko: 'PDF를 끌어다 놓을 수 있습니다', de: 'PDF kann hierher gezogen werden', fr: 'Vous pouvez glisser-déposer un PDF', es: 'Puedes arrastrar y soltar un PDF'},

  // --- ページナビ ---
  '◀ 前へ':               {en: '◀ Prev', zh: '◀ 上一页', ko: '◀ 이전', de: '◀ Zurück', fr: '◀ Préc.', es: '◀ Anterior'},
  '次へ ▶':               {en: 'Next ▶', zh: '下一页 ▶', ko: '다음 ▶', de: 'Weiter ▶', fr: 'Suiv. ▶', es: 'Siguiente ▶'},
  '移動':                 {en: 'Go', zh: '跳转', ko: '이동', de: 'Gehe zu', fr: 'Aller', es: 'Ir'},
  'ジャンプ':             {en: 'Jump to page', zh: '跳转到页', ko: '페이지로 이동', de: 'Zur Seite springen', fr: 'Aller à la page', es: 'Ir a la página'},
  '新ページに自動追従':   {en: 'Follow new pages', zh: '自动跟随新页面', ko: '새 페이지 자동 추적', de: 'Neuen Seiten folgen', fr: 'Suivre les nouvelles pages', es: 'Seguir páginas nuevas'},
  'PDFページ':            {en: 'PDF page', zh: 'PDF 页面', ko: 'PDF 페이지', de: 'PDF-Seite', fr: 'Page PDF', es: 'Página PDF'},
  'Ctrl+ホイールで拡大縮小': {en: 'Ctrl + wheel to zoom', zh: 'Ctrl+滚轮缩放', ko: 'Ctrl+휠로 확대/축소', de: 'Strg + Mausrad zum Zoomen', fr: 'Ctrl + molette pour zoomer', es: 'Ctrl + rueda para ampliar'},
  '縮小 (Ctrl+ホイール)': {en: 'Zoom out (Ctrl + wheel)', zh: '缩小（Ctrl+滚轮）', ko: '축소 (Ctrl+휠)', de: 'Verkleinern (Strg + Rad)', fr: 'Dézoomer (Ctrl + molette)', es: 'Reducir (Ctrl + rueda)'},
  '拡大 (Ctrl+ホイール)': {en: 'Zoom in (Ctrl + wheel)', zh: '放大（Ctrl+滚轮）', ko: '확대 (Ctrl+휠)', de: 'Vergrößern (Strg + Rad)', fr: 'Zoomer (Ctrl + molette)', es: 'Ampliar (Ctrl + rueda)'},
  '縮小':                 {en: 'Zoom out', zh: '缩小', ko: '축소', de: 'Verkleinern', fr: 'Dézoomer', es: 'Reducir'},
  '拡大':                 {en: 'Zoom in', zh: '放大', ko: '확대', de: 'Vergrößern', fr: 'Zoomer', es: 'Ampliar'},
  '等倍':                 {en: 'Actual size', zh: '原始大小', ko: '실제 크기', de: 'Originalgröße', fr: 'Taille réelle', es: 'Tamaño real'},

  // --- 訳文ペイン ---
  '日本語訳':             {en: 'Translation', zh: '译文', ko: '번역', de: 'Übersetzung', fr: 'Traduction', es: 'Traducción'},
  '訳文':                 {en: 'Translation', zh: '译文', ko: '번역', de: 'Übersetzung', fr: 'Traduction', es: 'Traducción'},
  '書体':                 {en: 'Typeface', zh: '字体', ko: '서체', de: 'Schriftart', fr: 'Police', es: 'Tipografía'},
  'ゴシック':             {en: 'Sans-serif', zh: '黑体', ko: '고딕', de: 'Serifenlos', fr: 'Sans empattement', es: 'Sans serif'},
  '明朝':                 {en: 'Serif', zh: '宋体', ko: '명조', de: 'Serif', fr: 'Avec empattement', es: 'Serif'},
  'システム標準':         {en: 'System default', zh: '系统默认', ko: '시스템 기본', de: 'Systemstandard', fr: 'Par défaut', es: 'Predeterminada'},
  '等幅':                 {en: 'Monospace', zh: '等宽', ko: '고정폭', de: 'Dicktengleich', fr: 'Chasse fixe', es: 'Monoespaciada'},
  '文字を小さく':         {en: 'Smaller text', zh: '缩小文字', ko: '글자 작게', de: 'Schrift kleiner', fr: 'Texte plus petit', es: 'Texto más pequeño'},
  '文字を大きく':         {en: 'Larger text', zh: '放大文字', ko: '글자 크게', de: 'Schrift größer', fr: 'Texte plus grand', es: 'Texto más grande'},
  'このページの図':       {en: 'Figures on this page', zh: '本页的图', ko: '이 페이지의 그림', de: 'Abbildungen auf dieser Seite', fr: 'Figures de cette page', es: 'Figuras de esta página'},

  // --- チャット ---
  '💬 質問チャット (このページの内容に基づいて回答)': {en: '💬 Ask about this document', zh: '💬 提问（基于文档内容回答）', ko: '💬 질문 채팅 (문서 내용에 근거해 답변)', de: '💬 Zum Dokument fragen', fr: '💬 Poser une question sur le document', es: '💬 Preguntar sobre el documento'},
  '折り畳み':             {en: 'Collapse', zh: '折叠', ko: '접기', de: 'Einklappen', fr: 'Replier', es: 'Contraer'},
  '展開':                 {en: 'Expand', zh: '展开', ko: '펼치기', de: 'Ausklappen', fr: 'Déplier', es: 'Expandir'},
  '範囲':                 {en: 'Scope', zh: '范围', ko: '범위', de: 'Bereich', fr: 'Portée', es: 'Ámbito'},
  '全ページ':             {en: 'Whole document', zh: '全部页面', ko: '전체 페이지', de: 'Ganzes Dokument', fr: 'Tout le document', es: 'Todo el documento'},
  '現在のページのみ':     {en: 'Current page only', zh: '仅当前页', ko: '현재 페이지만', de: 'Nur aktuelle Seite', fr: 'Page actuelle seulement', es: 'Solo la página actual'},
  '🌐 Web検索も使う':     {en: '🌐 Also search the web', zh: '🌐 同时使用网络搜索', ko: '🌐 웹 검색도 사용', de: '🌐 Auch im Web suchen', fr: '🌐 Chercher aussi sur le web', es: '🌐 Buscar también en la web'},
  '送信':                 {en: 'Send', zh: '发送', ko: '보내기', de: 'Senden', fr: 'Envoyer', es: 'Enviar'},
  'このページの内容について質問...': {en: 'Ask about this document…', zh: '就本文档内容提问…', ko: '이 문서 내용에 대해 질문…', de: 'Frage zum Dokument …', fr: 'Posez une question sur le document…', es: 'Pregunta sobre el documento…'},
  '会話の記憶':           {en: 'Memory', zh: '对话记忆', ko: '대화 기억', de: 'Gedächtnis', fr: 'Mémoire', es: 'Memoria'},
  '🔄 会話をリセット':    {en: '🔄 Reset conversation', zh: '🔄 重置对话', ko: '🔄 대화 초기화', de: '🔄 Gespräch zurücksetzen', fr: '🔄 Réinitialiser la conversation', es: '🔄 Reiniciar conversación'},
  '会話をリセットしました': {en: 'Conversation reset', zh: '已重置对话', ko: '대화를 초기화했습니다', de: 'Gespräch zurückgesetzt', fr: 'Conversation réinitialisée', es: 'Conversación reiniciada'},
  '直前の会話を踏まえて答えます。古い分は自動で忘れます': {en: 'Answers take the recent conversation into account; older turns are forgotten automatically', zh: '回答会参考最近的对话，较早的内容会自动遗忘', ko: '최근 대화를 바탕으로 답합니다. 오래된 내용은 자동으로 잊습니다', de: 'Antworten berücksichtigen das letzte Gespräch; ältere Runden werden automatisch vergessen', fr: 'Les réponses tiennent compte de la conversation récente ; les anciens échanges sont oubliés automatiquement', es: 'Las respuestas tienen en cuenta la conversación reciente; los turnos antiguos se olvidan automáticamente'},
  '会話の記憶を消して、翻訳した直後の状態から質問します': {en: 'Forget the conversation and ask from the state right after translation', zh: '清除对话记忆，从刚翻译完的状态重新提问', ko: '대화 기억을 지우고 번역 직후 상태에서 질문합니다', de: 'Gespräch vergessen und wieder vom Stand direkt nach der Übersetzung fragen', fr: 'Oublier la conversation et repartir de l’état juste après la traduction', es: 'Olvidar la conversación y preguntar desde el estado justo después de la traducción'},

  // --- 右クリックメニュー ---
  '🔄 再翻訳':            {en: '🔄 Retranslate', zh: '🔄 重新翻译', ko: '🔄 재번역', de: '🔄 Neu übersetzen', fr: '🔄 Retraduire', es: '🔄 Retraducir'},
  '← 前の文と結合':       {en: '← Merge with previous', zh: '← 与上一句合并', ko: '← 앞 문장과 결합', de: '← Mit vorherigem verbinden', fr: '← Fusionner avec la précédente', es: '← Unir con la anterior'},
  '次の文と結合 →':       {en: 'Merge with next →', zh: '与下一句合并 →', ko: '다음 문장과 결합 →', de: 'Mit nächstem verbinden →', fr: 'Fusionner avec la suivante →', es: 'Unir con la siguiente →'},
  '✂ 分割...':            {en: '✂ Split…', zh: '✂ 拆分…', ko: '✂ 분할…', de: '✂ Teilen …', fr: '✂ Scinder…', es: '✂ Dividir…'},
  '💬 コメントを追加/編集': {en: '💬 Add or edit a comment', zh: '💬 添加/编辑批注', ko: '💬 코멘트 추가/편집', de: '💬 Kommentar hinzufügen/bearbeiten', fr: '💬 Ajouter ou modifier un commentaire', es: '💬 Añadir o editar comentario'},
  '📋 原文をコピー':      {en: '📋 Copy the original', zh: '📋 复制原文', ko: '📋 원문 복사', de: '📋 Original kopieren', fr: '📋 Copier l’original', es: '📋 Copiar el original'},

  // --- コメント ---
  '💬 コメント':          {en: '💬 Comment', zh: '💬 批注', ko: '💬 코멘트', de: '💬 Kommentar', fr: '💬 Commentaire', es: '💬 Comentario'},
  'まだコメントはありません': {en: 'No comments yet', zh: '暂无批注', ko: '아직 코멘트가 없습니다', de: 'Noch keine Kommentare', fr: 'Aucun commentaire', es: 'Aún no hay comentarios'},
  'コメントを入力...':    {en: 'Write a comment…', zh: '输入批注…', ko: '코멘트를 입력…', de: 'Kommentar schreiben …', fr: 'Écrire un commentaire…', es: 'Escribe un comentario…'},
  '保存':                 {en: 'Save', zh: '保存', ko: '저장', de: 'Speichern', fr: 'Enregistrer', es: 'Guardar'},
  'キャンセル':           {en: 'Cancel', zh: '取消', ko: '취소', de: 'Abbrechen', fr: 'Annuler', es: 'Cancelar'},
  '閉じる':               {en: 'Close', zh: '关闭', ko: '닫기', de: 'Schließen', fr: 'Fermer', es: 'Cerrar'},
  '参考文献':             {en: 'Reference', zh: '参考文献', ko: '참고문헌', de: 'Literaturangabe', fr: 'Référence', es: 'Referencia'},

  // --- 再翻訳ダイアログ ---
  '🔄 再翻訳オプション':  {en: '🔄 Retranslation options', zh: '🔄 重新翻译选项', ko: '🔄 재번역 옵션', de: '🔄 Optionen für Neuübersetzung', fr: '🔄 Options de retraduction', es: '🔄 Opciones de retraducción'},
  '追加指示なしで再翻訳する場合は「そのまま再翻訳」を押してください。': {en: 'To retranslate without extra instructions, just press Retranslate.', zh: '若无需附加说明，直接点击「重新翻译」。', ko: '추가 지시 없이 재번역하려면 그대로 재번역을 누르세요.', de: 'Ohne zusätzliche Anweisungen einfach auf Neu übersetzen klicken.', fr: 'Pour retraduire sans consigne, cliquez simplement sur Retraduire.', es: 'Para retraducir sin instrucciones, pulsa Retraducir.'},
  '現在の訳:':            {en: 'Current translation:', zh: '当前译文：', ko: '현재 번역:', de: 'Aktuelle Übersetzung:', fr: 'Traduction actuelle :', es: 'Traducción actual:'},
  '前と違う訳にする (別の表現・語彙で)': {en: 'Use different wording from the previous translation', zh: '使用与上次不同的表达和词汇', ko: '이전 번역과 다른 표현・어휘로', de: 'Andere Formulierung als zuvor verwenden', fr: 'Utiliser une formulation différente', es: 'Usar una redacción distinta a la anterior'},
  '用語集 (任意)':        {en: 'Glossary (optional)', zh: '术语表（可选）', ko: '용어집 (선택)', de: 'Glossar (optional)', fr: 'Glossaire (facultatif)', es: 'Glosario (opcional)'},
  '1行に1件、`英単語 → 訳語` の形式で指定。例:': {en: 'One per line, as `source → translation`. For example:', zh: '每行一条，格式为 `原词 → 译词`。例如：', ko: '한 줄에 하나씩 `원어 → 번역어` 형식으로. 예:', de: 'Eine pro Zeile, als `Original → Übersetzung`. Beispiel:', fr: 'Un par ligne, sous la forme `source → traduction`. Exemple :', es: 'Uno por línea, como `origen → traducción`. Por ejemplo:'},
  'filament → フィラメント': {en: 'filament → filament', zh: 'filament → 丝状体', ko: 'filament → 필라멘트', de: 'filament → Filament', fr: 'filament → filament', es: 'filament → filamento'},
  '例:&#10;filament → フィラメント&#10;peptidase → ペプチダーゼ': {en: 'e.g.\nfilament → filament\npeptidase → peptidase', zh: '例如：\nfilament → 丝状体\npeptidase → 肽酶', ko: '예:\nfilament → 필라멘트\npeptidase → 펩티다아제', de: 'z. B.\nfilament → Filament\npeptidase → Peptidase', fr: 'ex. :\nfilament → filament\npeptidase → peptidase', es: 'p. ej.\nfilament → filamento\npeptidase → peptidasa'},
  '再翻訳を実行':         {en: 'Retranslate', zh: '执行重新翻译', ko: '재번역 실행', de: 'Neu übersetzen', fr: 'Retraduire', es: 'Retraducir'},

  // --- 分割ダイアログ ---
  '分割位置を指定':       {en: 'Choose where to split', zh: '指定拆分位置', ko: '분할 위치 지정', de: 'Trennstelle wählen', fr: 'Choisir où scinder', es: 'Elegir dónde dividir'},
  '分割したい位置に':     {en: 'Insert', zh: '在要拆分的位置插入', ko: '분할할 위치에', de: 'Fügen Sie', fr: 'Insérez', es: 'Inserta'},
  'を挿入してください (複数可)': {en: 'where you want to split (more than one is fine)', zh: '（可多处）', ko: '를 넣어 주세요 (여러 개 가능)', de: 'an der Trennstelle ein (auch mehrfach)', fr: 'aux endroits à scinder (plusieurs possibles)', es: 'donde quieras dividir (puedes poner varios)'},
  '分割して再翻訳':       {en: 'Split and retranslate', zh: '拆分并重新翻译', ko: '분할 후 재번역', de: 'Teilen und neu übersetzen', fr: 'Scinder et retraduire', es: 'Dividir y retraducir'},

  // --- モデル管理 ---
  '確認中...':            {en: 'Checking…', zh: '检查中…', ko: '확인 중…', de: 'Wird geprüft …', fr: 'Vérification…', es: 'Comprobando…'},
  'インストール済み':     {en: 'Installed', zh: '已安装', ko: '설치됨', de: 'Installiert', fr: 'Installés', es: 'Instalados'},
  '探索中...':            {en: 'Searching…', zh: '搜索中…', ko: '검색 중…', de: 'Suche läuft …', fr: 'Recherche…', es: 'Buscando…'},
  '追加できるモデル（視覚対応）': {en: 'Models you can add (vision-capable)', zh: '可添加的模型（支持视觉）', ko: '추가할 수 있는 모델 (시각 지원)', de: 'Verfügbare Modelle (mit Bildverarbeitung)', fr: 'Modèles disponibles (avec vision)', es: 'Modelos disponibles (con visión)'},
  '再探索':               {en: 'Rescan', zh: '重新搜索', ko: '다시 검색', de: 'Neu suchen', fr: 'Rechercher à nouveau', es: 'Buscar de nuevo'},
  '開始中...':            {en: 'Starting…', zh: '启动中…', ko: '시작 중…', de: 'Wird gestartet …', fr: 'Démarrage…', es: 'Iniciando…'},

  // --- 以下は JavaScript から t() で使う ---
  '起動':                 {en: 'Start', zh: '启动', ko: '시작', de: 'Starten', fr: 'Démarrer', es: 'Iniciar'},
  '停止':                 {en: 'Stop', zh: '停止', ko: '중지', de: 'Stoppen', fr: 'Arrêter', es: 'Detener'},
  '停止中...':            {en: 'Stopping…', zh: '停止中…', ko: '중지 중…', de: 'Wird gestoppt …', fr: 'Arrêt…', es: 'Deteniendo…'},
  '起動中...':            {en: 'Starting…', zh: '启动中…', ko: '시작 중…', de: 'Wird gestartet …', fr: 'Démarrage…', es: 'Iniciando…'},
  '使用中':               {en: 'In use', zh: '使用中', ko: '사용 중', de: 'In Benutzung', fr: 'Utilisé', es: 'En uso'},
  'ダウンロード':         {en: 'Download', zh: '下载', ko: '다운로드', de: 'Herunterladen', fr: 'Télécharger', es: 'Descargar'},
  'ダウンロード完了':     {en: 'Download finished', zh: '下载完成', ko: '다운로드 완료', de: 'Download abgeschlossen', fr: 'Téléchargement terminé', es: 'Descarga completada'},
  '接続中':               {en: 'Connected', zh: '已连接', ko: '연결됨', de: 'Verbunden', fr: 'Connecté', es: 'Conectado'},
  '未接続':               {en: 'Not connected', zh: '未连接', ko: '연결 안 됨', de: 'Nicht verbunden', fr: 'Non connecté', es: 'Sin conexión'},
  '視覚対応':             {en: 'Vision', zh: '支持视觉', ko: '시각 지원', de: 'Mit Bildverarbeitung', fr: 'Vision', es: 'Con visión'},
  '視覚なし・このアプリでは使えません': {en: 'No vision — cannot be used here', zh: '不支持视觉，本应用无法使用', ko: '시각 미지원 · 이 앱에서는 사용할 수 없습니다', de: 'Ohne Bildverarbeitung – hier nicht nutzbar', fr: 'Sans vision — inutilisable ici', es: 'Sin visión: no se puede usar aquí'},
  '導入済み':             {en: 'Installed', zh: '已安装', ko: '설치됨', de: 'Installiert', fr: 'Installé', es: 'Instalado'},
  'このアプリが起動':     {en: 'started by this app', zh: '由本应用启动', ko: '이 앱이 시작함', de: 'von dieser App gestartet', fr: 'lancé par cette application', es: 'iniciado por esta app'},
  '外部で起動中（systemd など）': {en: 'running externally (systemd, etc.)', zh: '在外部运行（systemd 等）', ko: '외부에서 실행 중 (systemd 등)', de: 'extern gestartet (z. B. systemd)', fr: 'lancé à l’extérieur (systemd, etc.)', es: 'iniciado por fuera (systemd, etc.)'},
  '起動している LLM が見つかりません': {en: 'no running LLM found', zh: '未找到正在运行的 LLM', ko: '실행 중인 LLM을 찾을 수 없습니다', de: 'kein laufendes LLM gefunden', fr: 'aucun LLM en cours trouvé', es: 'no se encontró ningún LLM en ejecución'},
  '見つかりません':       {en: 'not found', zh: '未找到', ko: '찾을 수 없음', de: 'nicht gefunden', fr: 'introuvable', es: 'no encontrado'},
  '(モデルなし)':         {en: '(no model)', zh: '（无模型）', ko: '(모델 없음)', de: '(kein Modell)', fr: '(aucun modèle)', es: '(sin modelo)'},
  '接続先':               {en: 'Endpoint', zh: '连接地址', ko: '연결 대상', de: 'Adresse', fr: 'Adresse', es: 'Destino'},
  'モデル置き場':         {en: 'Model folder', zh: '模型目录', ko: '모델 폴더', de: 'Modellordner', fr: 'Dossier des modèles', es: 'Carpeta de modelos'},
  'このアプリが起動した LLM を使用中です。': {en: 'Using the LLM started by this app.', zh: '正在使用本应用启动的 LLM。', ko: '이 앱이 시작한 LLM을 사용 중입니다.', de: 'Es wird das von dieser App gestartete LLM verwendet.', fr: 'Le LLM lancé par cette application est utilisé.', es: 'Se está usando el LLM iniciado por esta app.'},
  '「起動」を押すとアプリが llama-server を立ち上げて接続先を切り替えます。': {en: 'Press Start and the app launches llama-server and switches to it.', zh: '点击「启动」，应用会启动 llama-server 并切换连接。', ko: '"시작"을 누르면 앱이 llama-server를 띄우고 연결을 전환합니다.', de: 'Mit Starten startet die App llama-server und wechselt dorthin.', fr: 'En cliquant sur Démarrer, l’application lance llama-server et s’y connecte.', es: 'Al pulsar Iniciar, la app lanza llama-server y cambia la conexión.'},
  'GGUF が見つかりません。下のリストから追加してください。': {en: 'No GGUF found. Add one from the list below.', zh: '未找到 GGUF。请从下方列表添加。', ko: 'GGUF를 찾을 수 없습니다. 아래 목록에서 추가하세요.', de: 'Kein GGUF gefunden. Fügen Sie unten eines hinzu.', fr: 'Aucun GGUF trouvé. Ajoutez-en un dans la liste ci-dessous.', es: 'No se encontró ningún GGUF. Añade uno de la lista.'},
  'モデルを読み込んでいます（大きいモデルでは数分かかります）': {en: 'Loading the model (a few minutes for large ones)', zh: '正在加载模型（大模型需要数分钟）', ko: '모델을 불러오는 중입니다 (큰 모델은 몇 분 걸립니다)', de: 'Modell wird geladen (bei großen Modellen einige Minuten)', fr: 'Chargement du modèle (quelques minutes pour les gros modèles)', es: 'Cargando el modelo (unos minutos si es grande)'},
  'モデルを停止しました': {en: 'Model stopped', zh: '模型已停止', ko: '모델을 정지했습니다', de: 'Modell gestoppt', fr: 'Modèle arrêté', es: 'Modelo detenido'},
  '目安メモリ {n}GB 以上': {en: 'needs about {n}GB RAM', zh: '建议内存 {n}GB 以上', ko: '권장 메모리 {n}GB 이상', de: 'ca. {n} GB RAM nötig', fr: 'environ {n} Go de RAM', es: 'unos {n}GB de RAM'},

  // 進行状況
  '解析中...':            {en: 'Analysing…', zh: '解析中…', ko: '해석 중…', de: 'Analyse läuft …', fr: 'Analyse…', es: 'Analizando…'},
  'アップロード中...':    {en: 'Uploading…', zh: '上传中…', ko: '업로드 중…', de: 'Wird hochgeladen …', fr: 'Envoi…', es: 'Subiendo…'},
  '中止中...':            {en: 'Stopping…', zh: '中止中…', ko: '중지 중…', de: 'Wird abgebrochen …', fr: 'Arrêt…', es: 'Deteniendo…'},
  '翻訳中...':            {en: 'Translating…', zh: '翻译中…', ko: '번역 중…', de: 'Übersetzung läuft …', fr: 'Traduction…', es: 'Traduciendo…'},
  '翻訳待ち':             {en: 'Waiting', zh: '等待翻译', ko: '번역 대기', de: 'Wartet', fr: 'En attente', es: 'En espera'},
  '(翻訳待ち)':           {en: '(waiting)', zh: '（等待翻译）', ko: '(번역 대기)', de: '(wartet)', fr: '(en attente)', es: '(en espera)'},
  'ページ準備中':         {en: 'Preparing page', zh: '页面准备中', ko: '페이지 준비 중', de: 'Seite wird vorbereitet', fr: 'Préparation de la page', es: 'Preparando la página'},
  '(未翻訳)':             {en: '(not translated)', zh: '（未翻译）', ko: '(미번역)', de: '(nicht übersetzt)', fr: '(non traduit)', es: '(sin traducir)'},
  '(応答中...)':          {en: '(answering…)', zh: '（回答中…）', ko: '(응답 중…)', de: '(antwortet …)', fr: '(réponse…)', es: '(respondiendo…)'},
  '(空応答)':             {en: '(empty answer)', zh: '（空回复）', ko: '(빈 응답)', de: '(leere Antwort)', fr: '(réponse vide)', es: '(respuesta vacía)'},
  'あなた':               {en: 'You', zh: '你', ko: '나', de: 'Sie', fr: 'Vous', es: 'Tú'},
  'アシスタント':         {en: 'Assistant', zh: '助手', ko: '어시스턴트', de: 'Assistent', fr: 'Assistant', es: 'Asistente'},
  '完了 {done}/{total}':  {en: 'Done {done}/{total}', zh: '完成 {done}/{total}', ko: '완료 {done}/{total}', de: 'Fertig {done}/{total}', fr: 'Terminé {done}/{total}', es: 'Hechas {done}/{total}'},
  'ページ {page} / {total} を翻訳中 (完了 {done}/{total})': {en: 'Translating page {page} / {total} (done {done}/{total})', zh: '正在翻译第 {page} / {total} 页（已完成 {done}/{total}）', ko: '{total}쪽 중 {page}쪽 번역 중 (완료 {done}/{total})', de: 'Übersetze Seite {page} / {total} (fertig {done}/{total})', fr: 'Traduction de la page {page} / {total} (terminé {done}/{total})', es: 'Traduciendo la página {page} / {total} (hechas {done}/{total})'},
  'サプリ {page} / {total} を翻訳中 (完了 {done}/{total})': {en: 'Translating supplement {page} / {total} (done {done}/{total})', zh: '正在翻译补充材料 {page} / {total}（已完成 {done}/{total}）', ko: '부록 {page} / {total} 번역 중 (완료 {done}/{total})', de: 'Übersetze Supplement {page} / {total} (fertig {done}/{total})', fr: 'Traduction du supplément {page} / {total} (terminé {done}/{total})', es: 'Traduciendo el suplemento {page} / {total} (hechas {done}/{total})'},
  'サプリ 完了 {done}/{total}': {en: 'Supplement: done {done}/{total}', zh: '补充材料：完成 {done}/{total}', ko: '부록 완료 {done}/{total}', de: 'Supplement: fertig {done}/{total}', fr: 'Supplément : terminé {done}/{total}', es: 'Suplemento: hechas {done}/{total}'},

  // 操作の結果
  '原文をコピーしました': {en: 'Original copied', zh: '已复制原文', ko: '원문을 복사했습니다', de: 'Original kopiert', fr: 'Original copié', es: 'Original copiado'},
  'コメント削除':         {en: 'Comment deleted', zh: '批注已删除', ko: '코멘트 삭제', de: 'Kommentar gelöscht', fr: 'Commentaire supprimé', es: 'Comentario eliminado'},
  '削除しました':         {en: 'Deleted', zh: '已删除', ko: '삭제했습니다', de: 'Gelöscht', fr: 'Supprimé', es: 'Eliminado'},
  '再翻訳中...':          {en: 'Retranslating…', zh: '重新翻译中…', ko: '재번역 중…', de: 'Wird neu übersetzt …', fr: 'Retraduction…', es: 'Retraduciendo…'},
  '再翻訳完了':           {en: 'Retranslated', zh: '重新翻译完成', ko: '재번역 완료', de: 'Neu übersetzt', fr: 'Retraduit', es: 'Retraducido'},
  '編集中...':            {en: 'Editing…', zh: '编辑中…', ko: '편집 중…', de: 'Wird bearbeitet …', fr: 'Modification…', es: 'Editando…'},
  '編集+再翻訳 完了':     {en: 'Edited and retranslated', zh: '编辑并重新翻译完成', ko: '편집+재번역 완료', de: 'Bearbeitet und neu übersetzt', fr: 'Modifié et retraduit', es: 'Editado y retraducido'},
  '分割+再翻訳中...':     {en: 'Splitting and retranslating…', zh: '拆分并重新翻译中…', ko: '분할+재번역 중…', de: 'Teilen und neu übersetzen …', fr: 'Scission et retraduction…', es: 'Dividiendo y retraduciendo…'},
  '分割位置に | を入れてください': {en: 'Put | where you want to split', zh: '请在拆分位置插入 |', ko: '분할할 위치에 | 를 넣어 주세요', de: 'Setzen Sie | an die Trennstelle', fr: 'Placez | à l’endroit de la scission', es: 'Pon | donde quieras dividir'},
  'PDFを選択してください': {en: 'Choose a PDF first', zh: '请先选择 PDF', ko: 'PDF를 선택해 주세요', de: 'Bitte zuerst ein PDF wählen', fr: 'Choisissez d’abord un PDF', es: 'Elige primero un PDF'},
  '先にPDFを解析してください': {en: 'Analyse a PDF first', zh: '请先解析 PDF', ko: '먼저 PDF를 해석해 주세요', de: 'Analysieren Sie zuerst ein PDF', fr: 'Analysez d’abord un PDF', es: 'Analiza primero un PDF'},
  '現在の解析を中止しますか？途中結果は残ります。': {en: 'Stop the current analysis? What has been done so far is kept.', zh: '要中止当前解析吗？已完成的结果会保留。', ko: '현재 해석을 중지할까요? 진행된 결과는 남습니다.', de: 'Analyse abbrechen? Bisherige Ergebnisse bleiben erhalten.', fr: 'Arrêter l’analyse ? Les résultats obtenus sont conservés.', es: '¿Detener el análisis? Se conserva lo hecho hasta ahora.'},
  'このコメントを削除しますか?': {en: 'Delete this comment?', zh: '要删除这条批注吗？', ko: '이 코멘트를 삭제할까요?', de: 'Diesen Kommentar löschen?', fr: 'Supprimer ce commentaire ?', es: '¿Eliminar este comentario?'},
  '図が抽出されていません (0枚)': {en: 'No figures were extracted (0)', zh: '未提取到图（0 张）', ko: '그림이 추출되지 않았습니다 (0장)', de: 'Keine Abbildungen extrahiert (0)', fr: 'Aucune figure extraite (0)', es: 'No se extrajo ninguna figura (0)'},
  '表が抽出されていません (0枚)': {en: 'No tables were extracted (0)', zh: '未提取到表（0 个）', ko: '표가 추출되지 않았습니다 (0개)', de: 'Keine Tabellen extrahiert (0)', fr: 'Aucun tableau extrait (0)', es: 'No se extrajo ninguna tabla (0)'},
  '(キャプションなし)':   {en: '(no caption)', zh: '（无题注）', ko: '(캡션 없음)', de: '(keine Bildunterschrift)', fr: '(pas de légende)', es: '(sin pie)'},
  '※ この表はPDF内に画像として含まれていません(別ファイル提供など)。説明のみ表示します。': {en: 'Note: this table is not included in the PDF as an image (it may be supplied as a separate file). Only its description is shown.', zh: '注：该表未以图像形式包含在 PDF 中（可能以单独文件提供），仅显示说明。', ko: '※ 이 표는 PDF에 이미지로 포함되어 있지 않습니다(별도 파일 제공 등). 설명만 표시합니다.', de: 'Hinweis: Diese Tabelle ist nicht als Bild im PDF enthalten (evtl. separate Datei). Es wird nur die Beschreibung gezeigt.', fr: 'Remarque : ce tableau n’est pas inclus comme image dans le PDF (fourni à part). Seule la description est affichée.', es: 'Nota: esta tabla no está incluida como imagen en el PDF (puede venir aparte). Solo se muestra la descripción.'},
  '※ この図はPDF内から画像として取り出せませんでした。説明のみ表示します。': {en: 'Note: this figure could not be extracted as an image from the PDF. Only its description is shown.', zh: '注：无法从 PDF 中提取该图的图像，仅显示说明。', ko: '※ 이 그림은 PDF에서 이미지로 추출하지 못했습니다. 설명만 표시합니다.', de: 'Hinweis: Diese Abbildung konnte nicht als Bild aus dem PDF extrahiert werden. Es wird nur die Beschreibung gezeigt.', fr: 'Remarque : cette figure n’a pas pu être extraite du PDF. Seule la description est affichée.', es: 'Nota: no se pudo extraer esta figura del PDF. Solo se muestra la descripción.'},
};

let I18N_LANG = 'ja';

// 辞書にあれば訳を、無ければ日本語のまま返す
function t(s, params) {
  let out = s;
  if (I18N_LANG !== 'ja') {
    const e = I18N[s];
    if (e && e[I18N_LANG]) out = e[I18N_LANG];
  }
  if (params) {
    for (const k of Object.keys(params)) {
      out = out.split('{' + k + '}').join(params[k]);
    }
  }
  return out;
}

// 画面の静的な文字を差し替える。元の日本語はノードに覚えておき、
// 言語を切り替え直しても戻せるようにする。
function applyI18n(lang) {
  I18N_LANG = lang || 'ja';
  document.documentElement.lang = I18N_LANG;
  const skip = new Set(I18N_SKIP.map(id => document.getElementById(id)).filter(Boolean));
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      for (let p = node.parentNode; p; p = p.parentNode) {
        if (skip.has(p)) return NodeFilter.FILTER_REJECT;
        if (p.tagName === 'SCRIPT' || p.tagName === 'STYLE') return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    if (n.__i18nSrc === undefined) {
      const raw = n.nodeValue.trim();
      if (!raw || !I18N[raw]) continue;   // 辞書にない = 触らない
      n.__i18nSrc = raw;
      n.__i18nPad = [n.nodeValue.match(/^\s*/)[0], n.nodeValue.match(/\s*$/)[0]];
    }
    n.nodeValue = n.__i18nPad[0] + t(n.__i18nSrc) + n.__i18nPad[1];
  }
  for (const el of document.querySelectorAll('[placeholder],[title]')) {
    for (const attr of ['placeholder', 'title']) {
      const cur = el.getAttribute(attr);
      if (cur === null) continue;
      const key = '__i18n_' + attr;
      if (el[key] === undefined) {
        if (!I18N[cur]) continue;
        el[key] = cur;
      }
      el.setAttribute(attr, t(el[key]));
    }
  }
}
