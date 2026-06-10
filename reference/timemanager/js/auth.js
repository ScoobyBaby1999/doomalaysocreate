
// ===== USERS STORAGE =====
function getUsers() {
    return JSON.parse(localStorage.getItem("users") || "[]");
}

function saveUsers(users) {
    localStorage.setItem("users", JSON.stringify(users));
}

// ===== VALIDATION =====
function isValidEmail(email) {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

function isValidPassword(password) {
    return password.length >= 8 &&
        /[A-Z]/.test(password) &&
        /[a-z]/.test(password) &&
        /[0-9]/.test(password) &&
        /[^A-Za-z0-9]/.test(password);
}

// ===== UI HELPERS =====
function showError(message, color = "red") {
    const error = document.getElementById("error");
    if (!error) return;

    error.style.color = color;
    error.innerText = message;
}

// ===== SHOW / HIDE PASSWORD =====
function togglePassword() {
    const input = document.getElementById("password");
    if (!input) return;

    input.type = input.type === "password" ? "text" : "password";
}

// ===== REGISTER =====
function register() {
    const emailInput = document.getElementById("email");
    const passInput = document.getElementById("password");

    if (!emailInput || !passInput) return;

    const email = emailInput.value.trim();
    const password = passInput.value.trim();

    showError("");

    if (!isValidEmail(email)) {
        showError("Enter a valid email (example: user@email.com)");
        return;
    }

    if (!isValidPassword(password)) {
        showError("Password must be 8+ chars with uppercase, lowercase, number, and symbol");
        return;
    }

    let users = getUsers();

    if (users.find(u => u.email === email)) {
        showError("User already exists");
        return;
    }

    users.push({ email, password });
    saveUsers(users);

    showError("Account created successfully!", "lightgreen");
}

// ===== LOGIN =====
function login() {
    const emailInput = document.getElementById("email");
    const passInput = document.getElementById("password");

    if (!emailInput || !passInput) return;

    const email = emailInput.value.trim();
    const password = passInput.value.trim();

    showError("");

    if (!isValidEmail(email)) {
        showError("Invalid email format");
        return;
    }

    let users = getUsers();

    const user = users.find(u => u.email === email && u.password === password);

    if (!user) {
        showError("Invalid email or password");
        return;
    }

    // SAVE SESSION
    localStorage.setItem("user", email);

    // UX IMPROVEMENT
    showError("Login successful!", "lightgreen");

    // DELAY FOR FEEDBACK
    setTimeout(() => {
        window.location.href = "dashboard.html";
    }, 800);
}

// ===== LOGOUT =====
function logout() {
    localStorage.removeItem("user");
    localStorage.removeItem("userName");

    alert("You have been logged out");

    window.location.href = "home.html";
}

// ===== AUTO REDIRECT =====

// If user is already logged in → skip login page
if (window.location.pathname.includes("login.html")) {
    const user = localStorage.getItem("user");

    if (user) {
        window.location.href = "dashboard.html";
    }
}