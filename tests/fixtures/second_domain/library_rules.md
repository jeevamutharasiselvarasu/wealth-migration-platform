# Library business rules (the second domain's knowledge file)
If ReturnedDate is NULL, then Out Else Returned
Map 'LG' -> 'Large print'; anything else is rejected
Concatenate FirstName and LastName with a space
Copy the value where Branch = 'Main'
Target Return Status comes from Loan RETURNED_ON using the rule "If ReturnedDate is NULL, then Out Else Returned"
