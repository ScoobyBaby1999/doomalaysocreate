let notes = [];
let activeId = null;

function getUserKey() {
    const user = localStorage.getItem("user");
    return "notes_" + user;
}

function loadNotes() {
    notes = JSON.parse(localStorage.getItem(getUserKey()) || "[]");
}

function saveNotes() {
    localStorage.setItem(getUserKey(), JSON.stringify(notes));
}