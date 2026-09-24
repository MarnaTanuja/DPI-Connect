# Large Cross-DPI Synthetic Dataset

Each organization contains exactly 1,000 synthetic records.
Total: 5,000 records across 5 organizations.

The records are linked using the same citizen/property identifiers, so
the interoperability engine can test matching and transformation.

Organizations:
- CitizenRegistry: 1,000
- LandRegistryAuthority: 1,000
- TaxRegistry: 1,000
- MunicipalPropertyTaxDepartment: 1,000
- PublicBankRegistry: 1,000

Important:
- Data is synthetic and intended for development/testing.
- Within each organization, every record follows the same field names and structure.
- Across organizations, equivalent concepts use different field names, which is useful
  for testing schema mapping.
- IDs such as CID-00001 and survey numbers intentionally connect related records.

Useful mappings:
CitizenRegistry.citizen_id
  -> LandRegistryAuthority.citizen_id
  -> TaxRegistry.taxpayer_id
  -> MunicipalPropertyTaxDepartment.citizen_id
  -> PublicBankRegistry.customer_id

CitizenRegistry.legal_name
  -> LandRegistryAuthority.owner_name
  -> TaxRegistry.taxpayer_name
  -> MunicipalPropertyTaxDepartment.owner_name
  -> PublicBankRegistry.customer_name

LandRegistryAuthority.survey_number
  -> TaxRegistry.property_reference
  -> MunicipalPropertyTaxDepartment.property_reference

LandRegistryAuthority.assessed_value
  -> TaxRegistry.annual_property_value
