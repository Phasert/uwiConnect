function setQualificationSelection(faculty, savedFaculty, savedDepartment) {
  const department = document.getElementById(faculty.dataset.departmentSelect);
  const options = JSON.parse(faculty.dataset.qualificationOptions);
  faculty.querySelectorAll('[data-saved-option]').forEach(option => option.remove());
  const allFaculties = savedFaculty === 'All faculties' &&
    Array.from(faculty.options).some(option => option.value === savedFaculty);
  if (savedFaculty && !allFaculties && !Object.hasOwn(options, savedFaculty)) {
    const option = new Option(savedFaculty, savedFaculty);
    option.dataset.savedOption = '';
    faculty.add(option);
  }
  faculty.value = savedFaculty;
  if (allFaculties) savedDepartment = '';
  department.replaceChildren(new Option('Select a department', ''));
  const departments = Object.hasOwn(options, savedFaculty) ? options[savedFaculty] : [];
  departments.forEach(name => department.add(new Option(name, name)));
  if (savedDepartment && !departments.includes(savedDepartment)) {
    department.add(new Option(savedDepartment, savedDepartment));
  }
  department.value = savedDepartment;
  department.disabled = !savedFaculty || allFaculties;
}

document.querySelectorAll('[data-department-select]').forEach(faculty => {
  const department = document.getElementById(faculty.dataset.departmentSelect);
  setQualificationSelection(faculty, faculty.value, department.value);
  faculty.addEventListener('change', () => {
    setQualificationSelection(faculty, faculty.value, '');
  });
});
